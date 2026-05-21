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
                    price_source_label: 'local',
                    reason: '模型买入',
                    return_pct: 0,
                  }],
                },
              });
            """
        )

        self.assertIn("local", html)

    def test_holding_signal_change_html_only_says_changed(self):
        html = self._run_app_js(
            """
              signalChangesHtml({signal_changes: [
                {field: '模型信号', from: '观望', to: '卖出'},
                {field: '卖出信号', from: '低风险', to: '高风险'},
              ]});
            """
        )
        self.assertIn("模型信号、卖出信号有变更", html)
        self.assertNotIn("观望", html)
        self.assertNotIn("低风险", html)

    def test_market_indices_render_positive_negative_and_empty_states(self):
        html = self._run_app_js(
            """
              renderMarketIndices({data: [
                {code: '000001', short_name: '上证', price: 3120.42, change_pct: 0.38},
                {code: '399006', short_name: '创业板', price: 1910.10, change_pct: -0.21},
              ]});
              const filled = document.getElementById('marketIndices').innerHTML;
              renderMarketIndices({data: []});
              filled + '|' + document.getElementById('marketIndices').innerHTML;
            """
        )

        self.assertIn("上证", html)
        self.assertIn("3,120.42", html)
        self.assertIn("pos", html)
        self.assertIn("创业板", html)
        self.assertIn("neg", html)
        self.assertIn("指数暂不可用", html)

    def test_quote_link_opens_tencent_security_page_in_new_tab(self):
        html = self._run_app_js(
            """
              quoteLink({code: '159915'}, '创业板ETF', 'quote-link') +
              quoteLink({code: '510300'}, '510300', 'code quote-link');
            """
        )

        self.assertIn('href="https://gu.qq.com/sz159915"', html)
        self.assertIn('href="https://gu.qq.com/sh510300"', html)
        self.assertIn('target="_blank"', html)
        self.assertIn('rel="noopener noreferrer"', html)

    def test_render_rows_links_code_and_name_to_tencent_quotes(self):
        html = self._run_app_js(
            """
              renderRows([{
                code: '159915', name: '创业板ETF', category: '宽基', rank: 1,
                price: 1.234, change_pct: 1.2, ret3: 1, ret5: 2, ret10: 3,
                rsi: 55, vol_ratio: 1.1, score: 88,
              }]);
              document.getElementById('tbody').innerHTML;
            """
        )

        self.assertIn('href="https://gu.qq.com/sz159915"', html)
        self.assertIn('>159915</a>', html)
        self.assertIn('>创业板ETF</a>', html)

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

    def test_build_tabs_keeps_core_tabs_and_moves_categories_to_dropdown(self):
        html = self._run_app_js(
            """
              _holdings = new Set(['333333']);
              _activeCat = '科技';
              buildTabs([
                {code: '111111', category: '科技'},
                {code: '222222', category: '金融'},
                {code: '333333', category: '商品'},
              ]);
              document.getElementById('tabInner').innerHTML;
            """
        )

        self.assertIn('全部<span class="badge">3</span>', html)
        self.assertIn('自选<span class="badge">0</span>', html)
        self.assertIn('持仓<span class="badge">1</span>', html)
        self.assertIn('class="tab-select active"', html)
        self.assertIn('科技 (1)', html)
        self.assertIn('金融 (1)', html)
        self.assertNotIn('onclick="selectTab(\'科技\')"', html)


if __name__ == "__main__":
    unittest.main()
