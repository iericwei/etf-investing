import base64
import json
import subprocess
import textwrap
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APP_JS = ROOT / "web" / "static" / "app.js"


class FrontendCustomTabTests(unittest.TestCase):
    def _run_app_js(self, body: str) -> str:
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
              window: {{ addEventListener() {{}}, innerHeight: 800, innerWidth: 1200 }},
              document: {{
                addEventListener() {{}},
                body: {{ appendChild() {{}} }},
                querySelectorAll() {{ return []; }},
                querySelector(selector) {{
                  if (selector === '.table-wrap') return element('tableWrap');
                  return {{ style: {{}} }};
                }},
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
            const result = vm.runInContext({json.dumps(body)}, context);
            console.log(Buffer.from(String(result)).toString('base64'));
            """
        )
        proc = subprocess.run(
            ["node", "-e", script],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=True,
        )
        encoded = proc.stdout.strip().splitlines()[-1]
        return base64.b64decode(encoded).decode("utf-8")

    def test_custom_tab_filters_rows_added_by_user(self):
        result = self._run_app_js(
            """
              _allResults = [
                {code: '111111', category: '科技', is_custom: true},
                {code: '222222', category: '科技'},
                {code: '333333', category: '金融', is_custom: true},
              ];
              _activeCat = '自选';
              JSON.stringify(currentList().map(r => r.code));
            """
        )

        self.assertEqual(json.loads(result), ["111111", "333333"])

    def test_backtest_tooltip_shows_trade_point_price_source(self):
        html = self._run_app_js(
            """
              backtestCell({
                name: '测试ETF',
                backtest_return_pct: 2.34,
                backtest: {
                  scheme_display_name: '收盘前15分钟方案',
                  window_days: 22,
                  curve: [
                    {date: '2026-05-18', return_pct: 0},
                    {date: '2026-05-19', return_pct: 2.34},
                  ],
                  trade_points: [{
                    action: 'buy',
                    label: '买入（收盘前15分钟）',
                    date: '2026-05-18',
                    time: '14:45',
                    price: 1.234,
                    price_source_label: 'akshare 15分钟分时行情价',
                    reason: '模型买入',
                    return_pct: 0,
                  }],
                },
              });
            """
        )

        self.assertIn("akshare 15分钟分时行情价", html)

    def test_render_can_preserve_custom_tab_after_lightweight_refresh(self):
        result = self._run_app_js(
            """
              _activeCat = '自选';
              render({
                status: 'ready',
                results: [
                  {code: '111111', category: '科技'},
                  {code: '222222', category: '自选', is_custom: true},
                ],
                watchlist: ['222222'],
              }, true);
              _activeCat;
            """
        )

        self.assertEqual(result, "自选")


if __name__ == "__main__":
    unittest.main()
