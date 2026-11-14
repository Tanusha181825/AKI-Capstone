"""Synthetic end-to-end clinical edge cases; no patient data required."""
import importlib.util
from pathlib import Path
import tempfile
import unittest
import duckdb
import yaml

SOURCE=Path(__file__).resolve().parents[2]/'src/labels/build_final_labels.py'
spec=importlib.util.spec_from_file_location('final_labels',SOURCE)
module=importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class ClinicalEdges(unittest.TestCase):
    def test_end_to_end(self):
        with tempfile.TemporaryDirectory(prefix='kdigo_edges_') as td:
            root=Path(td)
            for sub in ['data/interim','data/reference','config','reports','raw/hosp','raw/icu']:
                (root/sub).mkdir(parents=True)
            (root/'config/paths.yaml').write_text(yaml.safe_dump({'datasets':{'mimic_root':str(root/'raw')}}))
            c=duckdb.connect(str(root/'data/reference/mimic_reference.duckdb'))
            c.execute('CREATE SCHEMA mimiciv_derived')
            c.execute("CREATE TABLE cohort AS SELECT 100+n AS subject_id,200+n AS hadm_id,n AS stay_id,TIMESTAMP '2200-01-01' AS intime,TIMESTAMP '2200-01-02' AS outtime,'MICU' AS first_careunit,'MICU' AS last_careunit,50 AS estimated_icu_age,24.0 AS icu_los_hours FROM range(1,17) t(n)")
            # Two stays of the same patient: no smoothing or carryover permitted.
            c.execute('UPDATE cohort SET subject_id=101 WHERE stay_id=2')
            def out(q,path,fmt='PARQUET'):
                c.execute(f"COPY ({q}) TO '{module.sqlpath(root/path)}' (FORMAT {fmt})")
            out('SELECT * FROM cohort','data/interim/cohort_candidates.parquet')
            c.execute("""CREATE TABLE mimiciv_derived.kdigo_creatinine AS
              SELECT hadm_id,stay_id,intime-INTERVAL '1 hour' charttime,
                CASE WHEN stay_id=6 THEN 0.0 ELSE 1.0 END creat,NULL::DOUBLE creat_low_past_48hr,NULL::DOUBLE creat_low_past_7day FROM cohort WHERE stay_id<>5
              UNION ALL SELECT hadm_id,stay_id,intime+INTERVAL '12 hours',
                CASE WHEN stay_id IN (1,3,7) THEN 2.0 WHEN stay_id=8 THEN 4.0 ELSE 1.0 END,
                CASE WHEN stay_id=5 THEN NULL WHEN stay_id=6 THEN 0.0 ELSE 1.0 END,
                CASE WHEN stay_id=5 THEN NULL WHEN stay_id=6 THEN 0.0 ELSE 1.0 END FROM cohort""")
            c.execute('ALTER TABLE mimiciv_derived.kdigo_creatinine ALTER creat TYPE DOUBLE')
            c.execute("UPDATE mimiciv_derived.kdigo_creatinine SET creat=CASE stay_id WHEN 13 THEN 1.3 WHEN 14 THEN 1.5 WHEN 15 THEN 2.999 WHEN 16 THEN 3.0 ELSE creat END WHERE charttime>TIMESTAMP '2200-01-01'")
            c.execute("INSERT INTO mimiciv_derived.kdigo_creatinine VALUES (209,9,TIMESTAMP '2200-01-01 20:00:00',2.0,1.0,1.0),(210,10,TIMESTAMP '2199-12-31 22:00:00',1.0,NULL,NULL)")
            c.execute("UPDATE mimiciv_derived.kdigo_creatinine SET creat=2.0,creat_low_past_48hr=1.0,creat_low_past_7day=1.0 WHERE stay_id=10 AND charttime>TIMESTAMP '2199-12-31 22:00:00'")
            out("""SELECT c.subject_id,r.hadm_id,r.charttime,
              r.charttime+CASE WHEN r.stay_id=7 THEN INTERVAL '3 hours' ELSE INTERVAL '0 hours' END storetime,
              50912 itemid,creat creatinine_original FROM mimiciv_derived.kdigo_creatinine r JOIN cohort c USING(stay_id)""",'data/interim/creatinine_reconciled_all.parquet')
            c.execute("CREATE TABLE mimiciv_derived.kdigo_stages AS SELECT c.subject_id,r.*,0 aki_stage,0 aki_stage_creat,NULL::INTEGER aki_stage_uo,NULL::INTEGER aki_stage_crrt FROM mimiciv_derived.kdigo_creatinine r JOIN cohort c USING(stay_id)")
            c.execute("CREATE TABLE mimiciv_derived.crrt(stay_id BIGINT,charttime TIMESTAMP,crrt_mode VARCHAR,system_active INTEGER)")
            out("SELECT 203 hadm_id,10 icd_version,'N186' icd_code",'raw/hosp/diagnoses_icd.csv.gz', 'CSV, COMPRESSION GZIP')
            out("SELECT 4 stay_id,TIMESTAMP '2200-01-01 14:00:00' starttime,TIMESTAMP '2200-01-01 15:00:00' endtime,TIMESTAMP '2200-01-01 16:00:00' storetime,225441 itemid,'FinishedRunning' statusdescription,1.0 AS \"value\"",'raw/icu/procedureevents.csv.gz','CSV, COMPRESSION GZIP')
            out("""SELECT stay_id,intime+INTERVAL '1 hour'*n AS charttime,
                intime+INTERVAL '1 hour'*(n+1) AS storetime,'retained' AS quality_flag
              FROM cohort CROSS JOIN range(0,15) x(n) WHERE stay_id IN (1,2,9,11,12)""",'data/interim/urine_reference_events.parquet')
            out("""SELECT stay_id,intime+INTERVAL '1 hour'*(n-1) interval_start,
                intime+INTERVAL '1 hour'*n interval_end,1.0 duration_hours,
                CASE WHEN stay_id IN (1,11,12) THEN 0.0 ELSE 50.0 END net_urine_ml
              FROM cohort CROSS JOIN range(1,15) x(n) WHERE stay_id IN (1,2,9,11,12) AND NOT(stay_id=11 AND n=6)""",'data/interim/urine_collection_intervals.parquet')
            out("SELECT stay_id,intime+INTERVAL '4 hours' charttime,intime+CASE WHEN stay_id=12 THEN INTERVAL '30 hours' ELSE INTERVAL '5 hours' END storetime,100.0 weight_kg_clean FROM cohort WHERE stay_id IN (1,2,9,11,12)",'data/interim/patient_weight_events.parquet')
            c.close()
            module.run(root)
            c=duckdb.connect()
            c.execute(f"CREATE VIEW s AS SELECT * FROM read_parquet('{module.sqlpath(root/'data/processed/aki_stay_labels.parquet')}')")
            c.execute(f"CREATE VIEW t AS SELECT * FROM read_parquet('{module.sqlpath(root/'data/processed/aki_timeline_labels.parquet')}')")
            self.assertEqual(c.execute('SELECT count(*) FROM s').fetchone()[0],16)
            self.assertEqual(c.execute('SELECT severe_aki FROM s WHERE stay_id=1').fetchone()[0],True)
            self.assertEqual(c.execute('SELECT max_kdigo_stage FROM s WHERE stay_id=2').fetchone()[0],0)
            self.assertEqual(c.execute('SELECT severe_aki FROM s WHERE stay_id=3').fetchone()[0],None)
            self.assertEqual(c.execute('SELECT max_observed_kdigo_stage,severe_aki FROM s WHERE stay_id=4').fetchone(),(3,None))
            self.assertEqual(c.execute('SELECT max_kdigo_stage FROM s WHERE stay_id=5').fetchone()[0],None)
            self.assertEqual(c.execute('SELECT max_kdigo_stage FROM s WHERE stay_id=6').fetchone()[0],None)
            self.assertEqual(c.execute("SELECT severe_aki_onset,severe_aki_available_onset FROM s WHERE stay_id=7").fetchone(),
                (module.duckdb.sql("SELECT TIMESTAMP '2200-01-01 12:00:00'").fetchone()[0],module.duckdb.sql("SELECT TIMESTAMP '2200-01-01 15:00:00'").fetchone()[0]))
            self.assertEqual(c.execute('SELECT max_kdigo_stage FROM s WHERE stay_id=8').fetchone()[0],3)
            self.assertEqual(c.execute('SELECT incident_severe_aki,eligible_incident_aki FROM s WHERE stay_id=9').fetchone(),(True,True))
            self.assertEqual(c.execute('SELECT incident_severe_aki,eligible_incident_aki FROM s WHERE stay_id=10').fetchone(),(False,False))
            self.assertEqual(c.execute('SELECT count(*) FROM t WHERE stay_id=11 AND aki_stage_uo>=2').fetchone()[0],0)  # missing hour cannot satisfy 12h
            self.assertEqual(c.execute('SELECT count(*) FROM t WHERE stay_id=12 AND aki_stage_uo IS NOT NULL').fetchone()[0],0)  # unavailable weight
            self.assertEqual(c.execute('SELECT max_kdigo_stage FROM s WHERE stay_id BETWEEN 13 AND 16 ORDER BY stay_id').fetchall(),[(1,),(1,),(2,),(3,)])
            self.assertEqual(c.execute('SELECT count(*) FROM t WHERE stay_id=2 AND aki_stage_uo>0').fetchone()[0],0)  # exactly .5 is not oliguria
            self.assertEqual(c.execute("SELECT min(charttime) FROM t WHERE stay_id=1 AND aki_stage_uo=3").fetchone()[0],module.duckdb.sql("SELECT TIMESTAMP '2200-01-01 12:00:00'").fetchone()[0])
            self.assertEqual(c.execute('SELECT count(*) FROM t WHERE weight_available_at>charttime').fetchone()[0],0)
            self.assertEqual(c.execute('SELECT count(*) FROM t WHERE severe_aki=false').fetchone()[0],0)
            with self.assertRaises(FileExistsError):
                module.run(root)
            c.close()


if __name__=='__main__':
    unittest.main()
