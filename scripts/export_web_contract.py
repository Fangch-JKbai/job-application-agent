"""Export the owning FastAPI OpenAPI contract without starting or querying a DB."""
import json
from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from api.main import app

if __name__=='__main__':
    (ROOT/'web/openapi.json').write_text(json.dumps(app.openapi(),ensure_ascii=False,indent=2)+'\n')
    print('Exported FastAPI contract to web/openapi.json')
