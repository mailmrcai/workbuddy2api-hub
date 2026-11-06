/* 全局提示词：五个并列模式的面板接线。

后端把模式改成五个并列选项（透传 / 追加 / 全部替换 / 精确替换 / 整段替换），
每个模式只读自己的输入：注入型用正文框，替换型用规则框。这里钉住：

  1. 下拉框五个 option 齐全，值与后端 mode 字符串逐字一致；
  2. 切换模式时只显示该模式要用到的那个输入框（正文框 / 规则框）；
  3. 读取设置时会把已存规则还原成「A => B」文本并同步显示态；
  4. 保存时提交 replace_rules，且替换型模式在规则为空时先拦住；
  5. 「替换 system」这个旧标签已经改成「全部替换」。

Run with Node: node tests/_test_prompt_modes_ui.js
*/
'use strict';

const assert = require('assert');
const {dashboardHtml} = require('./_dashboard_source.js');

const html = dashboardHtml();
let checks = 0;
function check(ok, msg){ assert.ok(ok, msg); checks += 1; }

// 1. 五个 option 齐全，值与后端一致
const MODE_VALUES = ['passthrough', 'append', 'custom', 'replace_exact', 'replace_paragraph'];
const select = html.match(/<select id="setGlobalPromptMode"[\s\S]*?<\/select>/);
check(select, '缺少 #setGlobalPromptMode 下拉框');
for(const value of MODE_VALUES){
  check(new RegExp('<option value="' + value + '">').test(select[0]),
        '下拉框缺少 option: ' + value);
}
check((select[0].match(/<option /g) || []).length === MODE_VALUES.length,
      '下拉框应恰好五个 option，不能多也不能少');
for(const value of MODE_VALUES){
  check(html.includes("'" + value + "'"), 'JS 里应出现模式字符串：' + value);
}

// 2. 按模式显示对应输入框
check(html.includes('id="setGlobalPromptBodyRow"'), '正文框应包在 #setGlobalPromptBodyRow 里');
check(html.includes('id="setGlobalPromptRulesRow"'), '规则框应包在 #setGlobalPromptRulesRow 里');
check(/function syncPromptModeUI\(\)/.test(html), '缺少 syncPromptModeUI');
check(/setGlobalPromptBodyRow[\s\S]{0,250}bodyRow\.style\.display = PROMPT_INJECT_MODES/.test(html),
      'syncPromptModeUI 应按注入型模式控制正文框');
check(/setGlobalPromptRulesRow[\s\S]{0,250}rulesRow\.style\.display = PROMPT_RULE_MODES/.test(html),
      'syncPromptModeUI 应按替换型模式控制规则框');
check(/<select id="setGlobalPromptMode" onchange="syncPromptModeUI\(\)"/.test(html),
      '下拉框应在 change 时同步显示态');

// 3. 读取设置：还原规则文本 + 同步显示态
check(/promptRulesInput\.value = Array\.isArray\(promptCfg\.replace_rules\)/
      .test(html), 'loadSettings 应把已存规则写回规则框');
check(/syncPromptModeUI\(\);[\s\S]{0,200}setGlobalPromptState/.test(html),
      'loadSettings 拉完配置应同步一次显示态');
check(/PROMPT_MODES\.indexOf\(promptCfg\.mode\) >= 0/.test(html),
      '未知/旧模式应回退到透传而不是留空');

// 4. 保存：提交规则、替换型空规则先拦住
check(/postJSON\('\/settings\/save', \{prompt: \{mode: mode, text: text, replace_rules: rules\}\}/
      .test(html), '保存应同时提交 mode / text / replace_rules');
check(/PROMPT_RULE_MODES\.indexOf\(mode\) >= 0 && rules\.length === 0/.test(html),
      '替换型模式在规则为空时应拒绝保存');
check(/\.indexOf\('=>'\)/.test(html), '规则解析应按首个 => 切分');
check(/String\(\(rulesEl && rulesEl\.value\) \|\| ''\)/.test(html),
      '规则框缺失时不该抛错（取不到值要给空串）');

// 5. 旧标签不再出现
check(!/>替换 system</.test(html), '「替换 system」旧标签应改成「全部替换」');
check(html.includes('"custom">全部替换') || html.includes("custom: '全部替换'")
      || /custom: '全部替换'/.test(html), 'custom 应在标签表里显示为「全部替换」');

console.log('PASS=' + checks + ' FAIL=0');
