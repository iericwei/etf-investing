import json
import subprocess
import textwrap
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APP_JS = ROOT / "web" / "static" / "app.js"


class FrontendCustomTabTests(unittest.TestCase):
    def test_custom_tab_filters_rows_added_by_user(self):
        script = textwrap.dedent(
            f"""
            const fs = require('fs');
            const vm = require('vm');
            const elements = new Map();
            function element(id) {{
              if (!elements.has(id)) {{
                elements.set(id, {{
                  id,
                  style: {{}},
                  innerHTML: '',
                  textContent: '',
                  disabled: false,
                  classList: {{ toggle() {{}} }},
                }});
              }}
              return elements.get(id);
            }}
            const context = {{
              console,
              window: {{ addEventListener() {{}} }},
              document: {{
                addEventListener() {{}},
                body: {{ appendChild() {{}} }},
                querySelectorAll() {{ return []; }},
                querySelector() {{ return {{ style: {{}} }}; }},
                getElementById: element,
              }},
              fetch: async (url) => ({{
                ok: true,
                json: async () => url === '/api/select'
                  ? {{ status: 'idle', results: [], watchlist: [] }}
                  : url === '/api/holdings'
                    ? {{ holdings: [] }}
                    : url === '/api/watchlist'
                      ? {{ watchlist: [] }}
                      : {{}},
              }}),
              setInterval() {{ return 0; }},
              clearInterval() {{}},
              setTimeout() {{ return 0; }},
              alert(msg) {{ throw new Error(msg); }},
            }};
            vm.createContext(context);
            vm.runInContext(fs.readFileSync({json.dumps(str(APP_JS))}, 'utf8'), context);
            const result = vm.runInContext(`
              _allResults = [
                {{code: '111111', category: '科技', is_custom: true}},
                {{code: '222222', category: '科技'}},
                {{code: '333333', category: '金融', is_custom: true}},
              ];
              _activeCat = '自选';
              JSON.stringify(currentList().map(r => r.code));
            `, context);
            console.log(result);
            """
        )
        proc = subprocess.run(
            ["node", "-e", script],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=True,
        )

        self.assertEqual(json.loads(proc.stdout.strip().splitlines()[-1]), ["111111", "333333"])


if __name__ == "__main__":
    unittest.main()
