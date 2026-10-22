"""Capture current offline outcomes and every skip reason without external opt-ins."""
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import unittest
import io
import json

if __name__=='__main__':
    root=Path(__file__).resolve().parents[1]
    stream=io.StringIO()
    suite=unittest.defaultTestLoader.discover(str(root),pattern='test*.py')
    result=unittest.TextTestRunner(stream=stream,verbosity=2).run(suite)
    out=root/'data/r6b_20261020'
    (out/'offline.log').write_text(stream.getvalue(),encoding='utf-8')
    stats={'total':result.testsRun,'passed':result.testsRun-len(result.skipped)-len(result.failures)-len(result.errors),'failed':len(result.failures),'errors':len(result.errors),'skipped':len(result.skipped),'skip_reasons':[{'test':test.id(),'reason':reason} for test,reason in result.skipped]}
    (out/'offline.json').write_text(json.dumps(stats,ensure_ascii=False,indent=2),encoding='utf-8')
    print({k:v for k,v in stats.items() if k!='skip_reasons'})
    sys.exit(not result.wasSuccessful())
