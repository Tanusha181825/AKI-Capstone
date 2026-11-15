"""Approved, conservative KDIGO labeling only. Never modifies reference tables.

Run with the repository venv; --root selects the existing aki-capstone repository.
Output publication refuses existing targets, including reports. See methodology.
"""
from pathlib import Path
import argparse
import hashlib
import json
import os
import time
import duckdb
import yaml


def sqlpath(p):
    return str(p).replace('\\', '/').replace("'", "''")


def creat_stage(value, low48, low7):
    if value is None or value <= 0 or value > 30:
        return None
    if (low48 is not None and low48 <= 0) or (low7 is not None and low7 <= 0):
        return None
    if low48 is None and low7 is None:
        return None
    if low7 is not None and value >= 3 * low7:
        return 3
    if value >= 4 and ((low48 is not None and value >= low48 + .3) or
                       (low7 is not None and value >= 1.5 * low7)):
        return 3
    if low7 is not None and value >= 2 * low7:
        return 2
    if ((low48 is not None and value >= low48 + .3) or
            (low7 is not None and value >= 1.5 * low7)):
        return 1
    return 0


def run(root):
    started = time.monotonic()
    processed = root / 'data/processed'
    reports = root / 'reports/labeling'
    targets = [processed / 'aki_stay_labels.parquet',
               processed / 'aki_timeline_labels.parquet']
    report_path = reports / 'validation.json'
    for p in targets + [report_path]:
        if p.exists():
            raise FileExistsError(f'Refusing overwrite: {p}')
    processed.mkdir(parents=True,exist_ok=True)
    reports.mkdir(parents=True,exist_ok=True)
    config_path = root / 'config/paths.yaml'
    if not config_path.exists():
        config_path = root / 'configs/paths.yaml'  # existing local audit compatibility
    cfg = yaml.safe_load(config_path.read_text())
    raw = Path(cfg['datasets']['mimic_root'])
    con = duckdb.connect(':memory:')
    con.execute('SET threads=4')
    con.execute("SET memory_limit='6GB'")
    con.execute(f"ATTACH '{sqlpath(root / 'data/reference/mimic_reference.duckdb')}' AS ref (READ_ONLY)")
    con.execute('CREATE SCHEMA audit')

    def execute(q):
        return con.execute(q)

    def stage(name, q):
        print(f'Building {name}', flush=True)
        execute(f'CREATE TABLE audit.{name} AS {q}')

    def parquet(name):
        return f"read_parquet('{sqlpath(root / 'data/interim' / (name + '.parquet'))}')"

    stage('cohort', f'SELECT * FROM {parquet("cohort_candidates")}')
    stage('chronic', f"""
      SELECT hadm_id, bool_or((icd_version=10 AND replace(icd_code,'.','') IN ('N186','Z992'))
        OR (icd_version=9 AND replace(icd_code,'.','') IN ('5856','V4511','V560','V568'))) AS chronic_dialysis_flag
      FROM read_csv_auto('{sqlpath(raw / 'hosp/diagnoses_icd.csv.gz')}') GROUP BY hadm_id
    """)
    # No giant raw labevents scan. Reconcile exact timestamp aggregates to existing extraction.
    stage('lab_times', f"""
      SELECT subject_id, charttime, avg(creatinine_original) AS raw_mean,
        max(greatest(charttime,storetime)) AS available_at,
        bool_and(storetime IS NOT NULL AND storetime>=charttime) AS timing_known
      FROM {parquet('creatinine_reconciled_all')}
      WHERE itemid=50912 AND creatinine_original IS NOT NULL AND creatinine_original<=150
      GROUP BY subject_id,charttime
    """)
    stage('cr0', """
      SELECT r.*, c.subject_id, c.intime, c.outtime,
        CASE WHEN abs(t.raw_mean-r.creat)<1e-8 AND t.timing_known THEN t.available_at END AS current_available_at
      FROM ref.mimiciv_derived.kdigo_creatinine r JOIN audit.cohort c USING(stay_id)
      LEFT JOIN audit.lab_times t ON t.subject_id=c.subject_id AND t.charttime=r.charttime
      WHERE r.charttime IS NOT NULL
    """)
    stage('cr', """
      WITH x AS (
        SELECT *, max(current_available_at) OVER w AS prior_available_at,
          count(*) FILTER(WHERE current_available_at IS NULL) OVER w AS prior_missing_time
        FROM audit.cr0
        WINDOW w AS (PARTITION BY stay_id ORDER BY charttime
          RANGE BETWEEN INTERVAL '7 days' PRECEDING AND INTERVAL '1 microsecond' PRECEDING)
      ) SELECT *, CASE
        WHEN creat<=0 OR creat>30 OR creat_low_past_7day<=0 OR creat_low_past_48hr<=0 THEN NULL
        WHEN creat_low_past_7day IS NULL AND creat_low_past_48hr IS NULL THEN NULL
        WHEN creat>=3*creat_low_past_7day THEN 3
        WHEN creat>=4 AND (creat>=creat_low_past_48hr+0.3 OR creat>=1.5*creat_low_past_7day) THEN 3
        WHEN creat>=2*creat_low_past_7day THEN 2
        WHEN creat>=creat_low_past_48hr+0.3 OR creat>=1.5*creat_low_past_7day THEN 1 ELSE 0 END AS stage,
        CASE WHEN current_available_at IS NOT NULL AND prior_missing_time=0
          THEN greatest(charttime,current_available_at,prior_available_at) END AS available_at
      FROM x
    """)
    stage('urine_times', f"""
      SELECT stay_id,charttime, max(greatest(charttime,storetime)) AS available_at,
        bool_and(storetime IS NOT NULL AND storetime>=charttime) AS timing_known,
        bool_or(quality_flag<>'retained') AS flagged_source
      FROM {parquet('urine_reference_events')} GROUP BY stay_id,charttime
    """)
    stage('intervals', f"""
      SELECT i.*, CASE WHEN u.timing_known AND p.timing_known
        THEN greatest(u.available_at,p.available_at) END AS available_at
      FROM {parquet('urine_collection_intervals')} i
      JOIN audit.urine_times u ON i.stay_id=u.stay_id AND i.interval_end=u.charttime
      JOIN audit.urine_times p ON i.stay_id=p.stay_id AND i.interval_start=p.charttime
      WHERE i.interval_start IS NOT NULL AND i.duration_hours>0 AND i.duration_hours<=3
        AND i.net_urine_ml>=0 AND NOT u.flagged_source AND NOT p.flagged_source
    """)
    stage('weights', f"""
      SELECT stay_id, greatest(charttime,storetime) AS available_at,
        arg_max(weight_kg_clean,charttime) AS weight_kg
      FROM {parquet('patient_weight_events')}
      WHERE weight_kg_clean IS NOT NULL AND storetime IS NOT NULL AND storetime>=charttime
      GROUP BY stay_id,greatest(charttime,storetime)
      HAVING min(weight_kg_clean)=max(weight_kg_clean)
    """)
    # Reuse interval reconstruction (36/37); full coverage replaces provisional 95%.
    stage('overlaps', """
      SELECT e.stay_id,e.interval_end AS charttime,i.net_urine_ml,i.duration_hours,i.available_at,
        greatest(0,epoch(least(i.interval_end,e.interval_end)-greatest(i.interval_start,e.interval_end-INTERVAL '6 hours')))/3600 AS h6,
        greatest(0,epoch(least(i.interval_end,e.interval_end)-greatest(i.interval_start,e.interval_end-INTERVAL '12 hours')))/3600 AS h12,
        greatest(0,epoch(least(i.interval_end,e.interval_end)-greatest(i.interval_start,e.interval_end-INTERVAL '24 hours')))/3600 AS h24
      FROM audit.intervals e JOIN audit.intervals i ON e.stay_id=i.stay_id
        AND i.interval_end>e.interval_end-INTERVAL '24 hours' AND i.interval_start<e.interval_end
        AND i.interval_end<=e.interval_end
    """)
    stage('rates', """
      SELECT stay_id,charttime,sum(h6) AS coverage6,sum(h12) AS coverage12,sum(h24) AS coverage24,
        sum(net_urine_ml*h6/duration_hours) AS volume6,
        sum(net_urine_ml*h12/duration_hours) AS volume12,
        sum(net_urine_ml*h24/duration_hours) AS volume24,
        CASE WHEN count(*) FILTER(WHERE h6>0 AND available_at IS NULL)=0
          THEN max(available_at) FILTER(WHERE h6>0) END AS available6,
        CASE WHEN count(*) FILTER(WHERE h12>0 AND available_at IS NULL)=0
          THEN max(available_at) FILTER(WHERE h12>0) END AS available12,
        CASE WHEN count(*) FILTER(WHERE h24>0 AND available_at IS NULL)=0
          THEN max(available_at) FILTER(WHERE h24>0) END AS available24
      FROM audit.overlaps GROUP BY stay_id,charttime
    """)
    stage('uo', """
      WITH weighted AS (
        SELECT r.*,w.weight_kg,w.available_at AS weight_available_at,
          CASE WHEN abs(coverage6-6)<1e-8 THEN volume6/(w.weight_kg*6) END AS rate6,
          CASE WHEN abs(coverage12-12)<1e-8 THEN volume12/(w.weight_kg*12) END AS rate12,
          CASE WHEN abs(coverage24-24)<1e-8 THEN volume24/(w.weight_kg*24) END AS rate24
        FROM audit.rates r ASOF LEFT JOIN audit.weights w
          ON r.stay_id=w.stay_id AND r.charttime>=w.available_at
      ), staged AS (
        SELECT *, CASE WHEN rate24<0.3 OR rate12=0 THEN 3 WHEN rate12<0.5 THEN 2
          WHEN rate6<0.5 THEN 1 WHEN rate6 IS NOT NULL THEN 0 END AS stage,
          CASE WHEN rate24<0.3 AND rate12=0 THEN least(available24,available12)
            WHEN rate24<0.3 THEN available24 WHEN rate12=0 OR rate12<0.5 THEN available12
            ELSE available6 END AS supporting_available_at
        FROM weighted
      ) SELECT *, CASE WHEN supporting_available_at IS NOT NULL
          THEN greatest(charttime,weight_available_at,supporting_available_at) END AS available_at
      FROM staged
    """)
    # RRT evidence is recorded but acute indication is not inferred from treatment alone.
    stage('rrt', f"""
      WITH p AS (
        SELECT stay_id,starttime AS charttime,
          CASE WHEN storetime IS NOT NULL AND storetime>=starttime THEN storetime END AS available_at,
          CASE itemid WHEN 225441 THEN 'hemodialysis' WHEN 225805 THEN 'peritoneal_dialysis' ELSE 'crrt_procedure' END AS source
        FROM read_csv_auto('{sqlpath(raw / 'icu/procedureevents.csv.gz')}')
        WHERE itemid IN (225802,225803,225809,225441,225805)
          AND statusdescription IN ('FinishedRunning','Stopped','Paused')
          AND value>0 AND endtime>starttime
        UNION ALL
        SELECT stay_id,charttime,NULL::TIMESTAMP,'crrt_chart'
        FROM ref.mimiciv_derived.crrt WHERE crrt_mode IS NOT NULL AND system_active=1
      ) SELECT stay_id,charttime,min(available_at) AS available_at,
        string_agg(DISTINCT source,'+' ORDER BY source) AS source,3 AS stage
      FROM p GROUP BY stay_id,charttime
    """)
    stage('times', """
      SELECT stay_id,charttime FROM ref.mimiciv_derived.kdigo_stages WHERE charttime IS NOT NULL
      UNION SELECT stay_id,charttime FROM audit.uo
      UNION SELECT stay_id,charttime FROM audit.rrt
    """)
    stage('timeline', """
      WITH joined AS (
        SELECT c.subject_id,c.hadm_id,c.stay_id,c.intime AS icu_intime,c.outtime AS icu_outtime,t.charttime,
          t.charttime BETWEEN c.intime AND c.outtime AS in_icu,
          coalesce(d.chronic_dialysis_flag,false) AS chronic_dialysis_flag,
          cr.creat,cr.creat_low_past_48hr,cr.creat_low_past_7day,
          cr.stage AS aki_stage_creat,u.stage AS aki_stage_uo,r.stage AS aki_stage_rrt,
          cr.available_at AS creat_available_at,u.available_at AS uo_available_at,r.available_at AS rrt_available_at,
          r.source AS rrt_source,u.weight_kg,u.weight_available_at,
          u.coverage6,u.coverage12,u.coverage24,u.rate6 AS uo_rt_6hr,u.rate12 AS uo_rt_12hr,u.rate24 AS uo_rt_24hr,
          s.aki_stage AS reference_aki_stage,s.aki_stage_creat AS reference_stage_creat,
          s.aki_stage_uo AS reference_stage_uo,s.aki_stage_crrt AS reference_stage_crrt,
          greatest(cr.stage,u.stage) AS nonrrt_stage
        FROM audit.times t JOIN audit.cohort c USING(stay_id)
        LEFT JOIN audit.chronic d USING(hadm_id)
        LEFT JOIN audit.cr cr ON t.stay_id=cr.stay_id AND t.charttime=cr.charttime
        LEFT JOIN audit.uo u ON t.stay_id=u.stay_id AND t.charttime=u.charttime
        LEFT JOIN audit.rrt r ON t.stay_id=r.stay_id AND t.charttime=r.charttime
        LEFT JOIN ref.mimiciv_derived.kdigo_stages s ON t.stay_id=s.stay_id AND t.charttime=s.charttime
      ) SELECT *,greatest(nonrrt_stage,aki_stage_rrt) AS observed_kdigo_stage,
        CASE WHEN NOT chronic_dialysis_flag THEN nonrrt_stage END AS aki_stage,
        CASE WHEN NOT chronic_dialysis_flag AND nonrrt_stage>=2 THEN true END AS severe_aki,
        CASE WHEN NOT chronic_dialysis_flag THEN least(
          CASE WHEN aki_stage_creat>=2 THEN creat_available_at END,
          CASE WHEN aki_stage_uo>=2 THEN uo_available_at END) END AS severe_evidence_available_at,
        concat_ws(';',CASE WHEN chronic_dialysis_flag THEN 'chronic_dialysis_or_esrd' END,
          CASE WHEN creat IS NOT NULL AND aki_stage_creat IS NULL THEN 'creatinine_unassessable' END,
          CASE WHEN aki_stage_rrt=3 THEN 'rrt_acute_indication_unconfirmed' END,
          CASE WHEN nonrrt_stage IS NULL THEN 'no_assessable_nonrrt_stage' END,
          CASE WHEN aki_stage_creat IS NOT NULL AND creat_available_at IS NULL THEN 'creatinine_availability_unknown' END,
          CASE WHEN aki_stage_uo IS NOT NULL AND uo_available_at IS NULL THEN 'urine_availability_unknown' END,
          'negative_outcome_not_established') AS label_quality_flag
      FROM joined
    """)
    stage('stay', """
      WITH agg AS (
        SELECT stay_id,max(aki_stage) FILTER(WHERE in_icu) AS max_kdigo_stage,
          max(observed_kdigo_stage) FILTER(WHERE in_icu) AS max_observed_kdigo_stage,
          min(charttime) FILTER(WHERE in_icu AND severe_aki) AS severe_aki_onset,
          min(severe_evidence_available_at) FILTER(WHERE in_icu AND severe_aki) AS severe_aki_available_onset,
          min(charttime) FILTER(WHERE NOT in_icu AND charttime<icu_intime AND nonrrt_stage>=2) AS pre_icu_severe_evidence,
          count(*) FILTER(WHERE in_icu) AS timeline_rows,
          count(*) FILTER(WHERE in_icu AND aki_stage_rrt=3) AS rrt_evidence_rows,
          bool_or(chronic_dialysis_flag) AS chronic_dialysis_flag
        FROM audit.timeline GROUP BY stay_id
      ), x AS (
        SELECT c.*,a.* EXCLUDE(stay_id),
          EXISTS(SELECT 1 FROM audit.timeline t WHERE t.stay_id=c.stay_id
            AND t.aki_stage_creat BETWEEN 0 AND 1 AND t.charttime<a.severe_aki_onset
            AND t.creat_available_at<=a.severe_aki_onset AND t.charttime>=c.intime) AS prior_nonsevere_creatinine,
          EXISTS(SELECT 1 FROM audit.timeline t WHERE t.stay_id=c.stay_id
            AND t.aki_stage_uo BETWEEN 0 AND 1 AND t.charttime<a.severe_aki_onset
            AND t.uo_available_at<=a.severe_aki_onset AND t.charttime>=c.intime) AS prior_nonsevere_urine,
          coalesce((SELECT string_agg(source,'+' ORDER BY source) FROM (
            SELECT 'creatinine' AS source FROM audit.timeline t WHERE t.stay_id=c.stay_id AND t.charttime=a.severe_aki_onset AND t.aki_stage_creat>=2
            UNION SELECT 'urine' FROM audit.timeline t WHERE t.stay_id=c.stay_id AND t.charttime=a.severe_aki_onset AND t.aki_stage_uo>=2)), 'unknown') AS onset_source
        FROM audit.cohort c LEFT JOIN agg a USING(stay_id)
      ) SELECT subject_id,hadm_id,stay_id,intime AS icu_intime,outtime AS icu_outtime,
        first_careunit,last_careunit,estimated_icu_age,icu_los_hours,
        max_kdigo_stage,max_observed_kdigo_stage,
        CASE WHEN max_kdigo_stage>=2 THEN true END AS severe_aki,
        severe_aki_onset,severe_aki_available_onset,pre_icu_severe_evidence,
        CASE WHEN severe_aki_onset=intime THEN true
          WHEN prior_nonsevere_creatinine AND prior_nonsevere_urine AND pre_icu_severe_evidence IS NULL THEN false END AS severe_aki_at_admission,
        CASE WHEN severe_aki_onset=intime OR pre_icu_severe_evidence IS NOT NULL OR chronic_dialysis_flag THEN false
          WHEN severe_aki_onset IS NOT NULL AND prior_nonsevere_creatinine AND prior_nonsevere_urine THEN true END AS incident_severe_aki,
        onset_source,prior_nonsevere_creatinine,prior_nonsevere_urine,
        coalesce(chronic_dialysis_flag,false) AS chronic_dialysis_flag,
        coalesce(rrt_evidence_rows,0) AS rrt_evidence_rows,coalesce(timeline_rows,0) AS timeline_rows,
        CASE WHEN chronic_dialysis_flag OR estimated_icu_age<18 OR outtime<=intime THEN false
          WHEN severe_aki_onset IS NOT NULL AND prior_nonsevere_creatinine AND prior_nonsevere_urine AND pre_icu_severe_evidence IS NULL THEN true
          ELSE false END AS eligible_incident_aki,
        concat_ws(';',CASE WHEN chronic_dialysis_flag THEN 'chronic_dialysis_or_esrd' END,
          CASE WHEN severe_aki_onset=intime OR pre_icu_severe_evidence IS NOT NULL THEN 'preexisting_severe_evidence' END,
          CASE WHEN severe_aki_onset IS NULL THEN 'negative_outcome_unverified'
            WHEN NOT(prior_nonsevere_creatinine AND prior_nonsevere_urine) THEN 'incident_status_unresolved' END) AS exclusion_reason,
        concat_ws(';',CASE WHEN severe_aki_onset IS NULL THEN 'outcome_unknown' END,
          CASE WHEN timeline_rows IS NULL OR timeline_rows=0 THEN 'no_in_icu_measurements' END,
          CASE WHEN rrt_evidence_rows>0 THEN 'rrt_indication_unconfirmed' END,
          CASE WHEN severe_aki_onset IS NOT NULL AND severe_aki_available_onset IS NULL THEN 'onset_availability_unknown' END,
          'intermittent_sampling;clinical_onset_is_first_documented_threshold;absence_not_proven') AS label_quality_flag,
        true AS onset_timing_uncertain
      FROM x
    """)
    report = {'policy_approved': '2026-11-14', 'checks': {}, 'counts': {}}

    def check(name,q):
        n=execute(q).fetchone()[0]
        report['checks'][name]=n
        if n:
            raise AssertionError(f'{name}: {n}')

    check('duplicate_stays', 'SELECT count(*)-count(DISTINCT stay_id) FROM audit.stay')
    check('duplicate_timeline', 'SELECT count(*) FROM (SELECT stay_id,charttime FROM audit.timeline GROUP BY ALL HAVING count(*)>1)')
    check('invalid_icu_intervals','SELECT count(*) FROM audit.stay WHERE icu_intime IS NULL OR icu_outtime<=icu_intime OR estimated_icu_age<18')
    check('invalid_stages','SELECT count(*) FROM audit.timeline WHERE aki_stage NOT BETWEEN 0 AND 3 OR observed_kdigo_stage NOT BETWEEN 0 AND 3')
    check('identifier_mismatch','SELECT count(*) FROM audit.timeline t JOIN audit.stay s USING(stay_id) WHERE t.subject_id<>s.subject_id OR t.hadm_id<>s.hadm_id')
    check('onset_mismatch', '''SELECT count(*) FROM audit.stay s LEFT JOIN
      (SELECT stay_id,min(charttime) FILTER(WHERE in_icu AND severe_aki) onset,max(aki_stage) FILTER(WHERE in_icu) stage FROM audit.timeline GROUP BY 1) t USING(stay_id)
      WHERE s.severe_aki_onset IS DISTINCT FROM t.onset OR s.max_kdigo_stage IS DISTINCT FROM t.stage''')
    check('severe_mismatch','SELECT count(*) FROM audit.stay WHERE severe_aki IS DISTINCT FROM CASE WHEN max_kdigo_stage>=2 THEN true END')
    check('onset_outside_icu','SELECT count(*) FROM audit.stay WHERE severe_aki_onset<icu_intime OR severe_aki_onset>icu_outtime')
    check('availability_before_event','SELECT count(*) FROM audit.timeline WHERE severe_evidence_available_at<charttime OR creat_available_at<charttime OR uo_available_at<charttime OR rrt_available_at<charttime')
    check('future_weight_used','SELECT count(*) FROM audit.timeline WHERE weight_available_at>charttime')
    check('incomplete_urine_stage2','SELECT count(*) FROM audit.timeline WHERE aki_stage_uo=2 AND abs(coverage12-12)>1e-8')
    check('incomplete_urine_stage3','SELECT count(*) FROM audit.timeline WHERE aki_stage_uo=3 AND NOT((abs(coverage12-12)<1e-8 AND uo_rt_12hr=0) OR (abs(coverage24-24)<1e-8 AND uo_rt_24hr<0.3))')
    check('chronic_dialysis_eligible','SELECT count(*) FROM audit.stay WHERE chronic_dialysis_flag AND (eligible_incident_aki OR severe_aki IS NOT NULL)')
    check('unproven_negatives','SELECT count(*) FROM audit.stay WHERE severe_aki=false')
    check('unsupported_incident','SELECT count(*) FROM audit.stay WHERE incident_severe_aki AND (NOT prior_nonsevere_creatinine OR NOT prior_nonsevere_urine OR pre_icu_severe_evidence IS NOT NULL)')
    check('nonpositive_creatinine_staged','SELECT count(*) FROM audit.cr WHERE stage IS NOT NULL AND (creat<=0 OR creat_low_past_7day<=0 OR creat_low_past_48hr<=0)')
    check('cohort_loss','SELECT abs((SELECT count(*) FROM audit.cohort)-(SELECT count(*) FROM audit.stay))')
    for name,q in {
      'stay_summary': '''SELECT count(*) stays,count(DISTINCT subject_id) patients,count(*) FILTER(WHERE severe_aki) severe,
        count(*) FILTER(WHERE severe_aki IS NULL) outcome_unknown,count(*) FILTER(WHERE incident_severe_aki) incident,
        count(*) FILTER(WHERE severe_aki_at_admission) confirmed_at_admission,count(*) FILTER(WHERE pre_icu_severe_evidence IS NOT NULL) pre_icu_severe,
        count(*) FILTER(WHERE eligible_incident_aki) eligible_incident,count(*) FILTER(WHERE chronic_dialysis_flag) chronic_dialysis_or_esrd,
        count(*) FILTER(WHERE timeline_rows=0) no_icu_evidence FROM audit.stay''',
      'stage_distribution': 'SELECT max_kdigo_stage,count(*) n FROM audit.stay GROUP BY 1 ORDER BY 1',
      'exclusions': 'SELECT exclusion_reason,count(*) n FROM audit.stay GROUP BY 1 ORDER BY 2 DESC',
      'onset_sources': 'SELECT onset_source,count(*) n FROM audit.stay WHERE severe_aki GROUP BY 1',
      'availability': 'SELECT count(*) FILTER(WHERE severe_aki AND severe_evidence_available_at IS NULL) unknown_severe_time,count(*) FILTER(WHERE severe_evidence_available_at>charttime) delayed_severe_rows,count(*) timeline_rows FROM audit.timeline',
      'reference_comparison': '''SELECT s.aki_stage>=2 reference_severe,t.aki_stage>=2 approved_severe,count(*) n
        FROM ref.mimiciv_derived.kdigo_stages s JOIN audit.timeline t USING(stay_id,charttime) WHERE t.in_icu GROUP BY 1,2''',
      'reference_counts': "SELECT table_name,estimated_size FROM duckdb_tables() WHERE database_name='ref'",
    }.items():
        result=execute(q)
        cols=[d[0] for d in result.description]
        report['counts'][name]=[dict(zip(cols,row)) for row in result.fetchall()]
    report['seconds']=round(time.monotonic()-started,2)
    report['limitations'] = [
      'No true negative outcomes asserted: intermittent measurements do not prove absence across the entire stay.',
      'Incident means first observed severe evidence after earlier nonsevere creatinine AND urine evidence, not proof of biological onset.',
      'RRT stage 3 is observed evidence only; acute indication/chronicity not inferred. Confirmed severe_aki requires independent non-RRT severe evidence.',
      'Discharge ESRD/dialysis codes are retrospective exclusions, never online features; absence of codes does not establish no chronic dialysis.',
      'Clinical charttime is retrospective; do not use a stage before its criterion-specific available_at. Unknown availability is not assumed timely.',
      'Reference creatinine observations begin seven days before ICU admission, truncating earlier rolling baselines; no imputation.',
      'Uniform volume allocation within inferred urine intervals is an assumption; full coverage does not establish continuous observation.',
      'RRT SCUF omitted because isolated ultrafiltration does not establish acute kidney injury.',
    ]
    # Write candidates, verify persisted schemas/counts, then publish with exclusive links.
    pending=[]
    try:
        for table,target in zip(['stay','timeline'],targets):
            tmp=target.with_suffix('.parquet.pending')
            if tmp.exists():
                raise FileExistsError(tmp)
            pending.append(tmp)
            execute(f"COPY audit.{table} TO '{sqlpath(tmp)}' (FORMAT PARQUET,COMPRESSION ZSTD)")
            check(f'{table}_roundtrip_count',f"SELECT abs((SELECT count(*) FROM audit.{table})-(SELECT count(*) FROM read_parquet('{sqlpath(tmp)}')))")
        report['outputs']={}
        for p,t in zip(targets,pending):
            with t.open('rb') as f:
                digest=hashlib.file_digest(f,'sha256').hexdigest()
            report['outputs'][p.relative_to(root).as_posix()]={'bytes':t.stat().st_size,'sha256':digest}
        for tmp,target in zip(pending,targets):
            os.link(tmp,target)  # exclusive, fails if a target appeared concurrently
        with report_path.open('x',encoding='utf-8') as f:
            json.dump(report,f,indent=2,default=str)
        print(json.dumps(report,indent=2,default=str),flush=True)
    finally:
        for tmp in pending:
            if tmp.exists():
                tmp.unlink()
        con.close()


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--root',type=Path,default=Path(__file__).resolve().parents[2])
    args=parser.parse_args()
    run(args.root)
