"use strict";

// This example is synthetic. It never sends an HTTP request or calls an SDK.
const MODEL = "claude-opus-5-5-high";
const schema = {
  type: "object",
  properties: { a: { type: "number" }, b: { type: "number" } },
  required: ["a", "b"],
  additionalProperties: false,
};
const functionTool = {
  type: "function", name: "add_numbers",
  description: "Add two numbers.", parameters: schema,
};
const messageTool = {
  name: "add_numbers", description: "Add two numbers.", input_schema: schema,
};
const callId = "toolu_demo_01";
const call = { type: "function_call", call_id: callId, name: "add_numbers", arguments: '{"a":2,"b":3}' };
const toolUse = { type: "tool_use", id: callId, name: "add_numbers", input: { a: 2, b: 3 } };
const exampleRequest = {
  responses: {
    model: MODEL, instructions: "Use the supplied tool.",
    input: [{ role: "user", content: "Add 2 and 3." }],
    tools: [functionTool], stream: true,
  },
  messages: {
    model: MODEL, max_tokens: 256, system: "Use the supplied tool.",
    messages: [{ role: "user", content: "Add 2 and 3." }],
    tools: [messageTool], stream: true,
  },
};
const normalized = {
  model: MODEL, instructions: "Use the supplied tool.",
  input: [{ role: "user", content: [{ type: "input_text", text: "Add 2 and 3." }] }],
  tools: [functionTool], tool_choice: "auto", parallel_tool_calls: true, store: false,
};
const responseContinuation = {
  model: MODEL, instructions: "Use the supplied tool.", previous_response_id: "resp_demo_01",
  input: [{ type: "function_call_output", call_id: callId, output: "5" }],
  tools: [functionTool], stream: true,
};
const messageContinuation = {
  ...exampleRequest.messages,
  messages: [
    { role: "user", content: "Add 2 and 3." },
    { role: "assistant", content: [toolUse] },
    { role: "user", content: [{ type: "tool_result", tool_use_id: callId, content: "5" }] },
  ],
};
const sdkSketch = `# SDK call sketch; supporting definitions omitted.
options = AgentOptions(
    model={
        "id": "claude-opus-5-5",
        "params": [
            {"id": "context", "value": "1m"},
            {"id": "effort", "value": "high"},
            {"id": "fast", "value": "false"}
        ]
    },
    tools=["mcp"],
    disallowed_tools=[
        "shell", "read", "edit", "task",
        "webSearch", "webFetch"
    ],
    local=LocalAgentOptions(
        cwd=empty_workspace,
        setting_sources=[],
        store={"type": "sqlite"},
        custom_tools={
            "add_numbers": CustomTool(
                input_schema=schema,
                execute=wait_for_client
            )
        }
    )
)
agent = await client.agents.create(options)
run = await agent.send(
    native_prompt,
    SendOptions(on_delta=observe)
)`;

const commonStages = [
  {
    titleEn: "The client sends a request.", titleZh: "客户端发出请求。",
    en: "The request identifies a model and a tool. The bridge reads the instructions and conversation.",
    zh: "请求指定模型和加法工具。桥接器读取指令、对话内容和参数定义。",
    state: "not_started",
  },
  {
    titleEn: "The bridge validates the data.", titleZh: "桥接器统一格式并校验。",
    en: "Messages becomes an internal request. The two APIs use the same history and tool checks.",
    zh: "Messages 先转成类似 Responses 的内部请求；两个 API 共用历史与工具校验。",
    state: "validated",
  },
  {
    titleEn: "The SDK starts a run.", titleZh: "SDK 启动一次运行。",
    en: "The bridge adds the tool callback to the SDK tool list. A new agent receives the instructions and history. The model can call the registered tool.",
    zh: "桥接器注册工具回调，用指令和历史启动新 agent。模型可以调用该工具；SDK 内置工具保持关闭。",
    state: "running",
  },
  {
    titleEn: "The callback waits.", titleZh: "回调等待客户端结果。",
    en: "The bridge sends the tool call. The HTTP response ends. A Future keeps the SDK callback open.",
    zh: "桥接器返回工具调用并结束本次 HTTP 响应。Future 仍在等待，因此 SDK 运行尚未结束。",
    state: "awaiting_tool_results",
  },
  {
    titleEn: "The client sends the result back.", titleZh: "客户端回传执行结果。",
    en: "The client calculates the sum. The bridge validates the call ID and completes the Future. The same SDK run continues.",
    zh: "客户端算出结果 5。桥接器检查调用 ID 和待处理批次，完成 Future，让原 SDK 运行继续。",
    state: "running / same run",
  },
  {
    titleEn: "The bridge sends the answer.", titleZh: "桥接器发出最终回答。",
    en: "The SDK run ends. The bridge validates the result and model identity. It sends the text in the client's format.",
    zh: "SDK 成功结束。桥接器检查完成状态与模型身份，将文本封装成客户端所需格式。",
    state: "finished",
  },
];

let protocol = "responses";
let step = 0;
let timer = null;
let currentCode = "";
let copyReset = null;
const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];
const escapeHTML = (value) => String(value).replace(/[&<>"']/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[char]));

function highlightJSON(value) {
  const pattern = /("(?:\\.|[^"\\])*"\s*:?)|\b(true|false|null|\d+(?:\.\d+)?)\b/g;
  let html = "", end = 0;
  for (const match of value.matchAll(pattern)) {
    html += escapeHTML(value.slice(end, match.index));
    const kind = match[1] ? (match[0].endsWith(":") ? "key" : "string") : "number";
    html += `<span class="${kind}">${escapeHTML(match[0])}</span>`;
    end = match.index + match[0].length;
  }
  return html + escapeHTML(value.slice(end));
}

function stageData() {
  const data = { ...commonStages[step] };
  const isMessages = protocol === "messages";
  let value;
  if (step === 0) {
    value = exampleRequest[protocol];
    data.label = isMessages ? "POST /v1/messages" : "POST /v1/responses";
    if (isMessages) {
      data.en += " This bridge does not forward max_tokens to the SDK.";
      data.zh += "此示例保留客户端常见的 max_tokens 字段，但桥接器不会将其传给 SDK。";
    }
  } else if (step === 1) {
    if (isMessages) {
      value = normalized;
      data.label = "prepare_messages → internal request";
      data.en = "system becomes instructions. input_schema becomes parameters. The bridge sets store to false. This object stays inside the bridge.";
      data.zh = "system 转成 instructions，input_schema 转成 parameters，并设置 store=false。右侧对象只在桥接器内部使用。";
    } else {
      value = {
        instructions: exampleRequest.responses.instructions,
        input: exampleRequest.responses.input,
        tools: [functionTool], tool_choice: "auto", parallel_tool_calls: true,
      };
      data.label = "prepare_request → checked payload";
      data.en = "The bridge validates the model and tool schema. It keeps the message order. Missing tool policy fields use their defaults.";
      data.zh = "桥接器校验模型和工具 schema，保留消息顺序，为缺省的工具策略填入默认值。native 引擎随后单独渲染 SDK 提示文本。";
    }
  } else if (step === 2) {
    data.label = "Python SDK · illustrative call sequence";
    data.code = sdkSketch;
    data.plain = true;
  } else if (step === 3) {
    value = isMessages ? {
      id: "msg_demo_01", type: "message", role: "assistant", content: [toolUse], stop_reason: "tool_use",
    } : {
      id: "resp_demo_01", object: "response", status: "completed",
      output: [{ ...call, id: "fc_demo_01", status: "completed" }],
    };
    data.label = "Response excerpt · metadata omitted";
    if (!isMessages) {
      data.en += " Here, completed is the status of the HTTP response segment.";
      data.zh += "此处 completed 表示当前响应片段完成。";
    }
  } else if (step === 4) {
    value = isMessages ? messageContinuation : responseContinuation;
    data.label = isMessages ? "POST /v1/messages · continuation" : "POST /v1/responses · continuation";
    data.en += isMessages ? " The request includes the full message history." : " The cached response supplies the earlier history.";
    data.zh += isMessages ? "请求携带完整消息历史。" : "previous_response_id 从本地缓存补全之前的历史；缓存失效时需要完整历史。";
  } else {
    value = isMessages ? {
      id: "msg_demo_02", type: "message", role: "assistant",
      content: [{ type: "text", text: "The sum is 5." }], stop_reason: "end_turn",
    } : {
      id: "resp_demo_02", object: "response", status: "completed",
      output: [{ id: "msg_demo_02", type: "message", role: "assistant", status: "completed",
        content: [{ type: "output_text", text: "The sum is 5.", annotations: [], logprobs: [] }] }],
    };
    data.label = "Final response excerpt · metadata omitted";
  }
  data.code = data.code || JSON.stringify(value, null, 2);
  return data;
}

function render() {
  const data = stageData();
  currentCode = data.code;
  $("#stage-copy").innerHTML = `<div class="stage-counter">STEP ${String(step + 1).padStart(2, "0")} / 06</div>
    <h3><span lang="en">${escapeHTML(data.titleEn)}</span><span lang="zh-CN">${escapeHTML(data.titleZh)}</span></h3>
    <p lang="en">${escapeHTML(data.en)}</p><p class="zh" lang="zh-CN">${escapeHTML(data.zh)}</p>
    <div class="stage-state"><div class="state-label">SDK RUN STATE</div><span class="state-value"><i class="dot"></i>${escapeHTML(data.state)}</span></div>`;
  $("#code-label").textContent = data.label;
  $("#stage-code").innerHTML = data.plain ? escapeHTML(data.code) : highlightJSON(data.code);
  $(".code-scroll").scrollTop = 0;
  $("#previous").disabled = step === 0;
  $("#next").innerHTML = step === 5 ? '<span lang="en">Start again</span><span lang="zh-CN">重新开始</span> ↺' : '<span lang="en">Next step</span><span lang="zh-CN">下一步</span> →';
  $$('[data-step]').forEach((button) => {
    const n = Number(button.dataset.step);
    if (n === step) button.setAttribute("aria-current", "step"); else button.removeAttribute("aria-current");
    button.classList.toggle("visited", n < step);
  });
  $("#step-progress").innerHTML = Array.from({ length: 6 }, (_, n) => `<i class="${n <= step ? "on" : ""}"></i>`).join("");
  $("#copy-code").textContent = "COPY";
  if (copyReset) clearTimeout(copyReset);
}

function stop() {
  if (timer) clearInterval(timer);
  timer = null;
  $("#play").innerHTML = '<span lang="en">Play</span><span lang="zh-CN">播放流程</span>';
}

$$('[data-protocol]').forEach((button) => button.addEventListener("click", () => {
  stop(); protocol = button.dataset.protocol; step = 0;
  $$('[data-protocol]').forEach((item) => item.setAttribute("aria-pressed", String(item === button)));
  render();
}));
$$('[data-step]').forEach((button) => button.addEventListener("click", () => { stop(); step = Number(button.dataset.step); render(); }));
$("#previous").addEventListener("click", () => { stop(); step = Math.max(0, step - 1); render(); });
$("#next").addEventListener("click", () => { stop(); step = (step + 1) % 6; render(); });
$("#play").addEventListener("click", () => {
  if (timer) { stop(); return; }
  if (step === 5) { step = 0; render(); }
  $("#play").innerHTML = '<span lang="en">Pause</span><span lang="zh-CN">暂停</span>';
  timer = setInterval(() => { step += 1; render(); if (step === 5) stop(); }, 2600);
});
document.addEventListener("visibilitychange", () => { if (document.hidden) stop(); });

function applyLanguage(language) {
  if (!["en", "zh", "dual"].includes(language)) language = "dual";
  document.body.dataset.language = language;
  document.documentElement.lang = language === "en" ? "en" : "zh-CN";
  $$("button[data-language]").forEach((button) => button.setAttribute("aria-pressed", String(button.dataset.language === language)));
}
$$("button[data-language]").forEach((button) => button.addEventListener("click", () => {
  applyLanguage(button.dataset.language);
  try { localStorage.setItem("cursor-bridge-guide-language", button.dataset.language); } catch { /* Storage can be disabled. */ }
}));
try { applyLanguage(localStorage.getItem("cursor-bridge-guide-language") || "dual"); } catch { applyLanguage("dual"); }

$("#copy-code").addEventListener("click", async () => {
  let copied = false;
  try {
    if (window.isSecureContext && navigator.clipboard) {
      await navigator.clipboard.writeText(currentCode);
      copied = true;
    }
  } catch { /* Plain HTTP uses the fallback below. */ }
  if (!copied) {
    const area = document.createElement("textarea");
    area.value = currentCode;
    area.className = "sr-only";
    document.body.append(area);
    area.select();
    try { copied = document.execCommand("copy"); } catch { /* Show a manual-copy hint. */ }
    area.remove();
    $("#copy-code").focus({ preventScroll: true });
  }
  $("#copy-code").textContent = copied ? "COPIED ✓" : "SELECT TEXT";
  copyReset = setTimeout(() => { $("#copy-code").textContent = "COPY"; }, 1800);
});

$("#print").addEventListener("click", () => window.print());
// Include closed reference tables and notes in the printed guide.
let closedBeforePrint = [];
window.addEventListener("beforeprint", () => {
  stop(); closedBeforePrint = $$("details:not([open])");
  closedBeforePrint.forEach((item) => { item.open = true; });
});
window.addEventListener("afterprint", () => { closedBeforePrint.forEach((item) => { item.open = false; }); });

const sections = $$("main section[id]");
const links = $$(".nav-links a");
function updateNavigation() {
  let active = sections[0].id;
  sections.forEach((section) => { if (section.getBoundingClientRect().top <= 170) active = section.id; });
  links.forEach((link) => {
    const selected = link.hash === `#${active}`;
    link.classList.toggle("active", selected);
    if (selected) link.setAttribute("aria-current", "location"); else link.removeAttribute("aria-current");
  });
}
let scrollPending = false;
document.addEventListener("scroll", () => {
  if (!scrollPending) {
    scrollPending = true;
    requestAnimationFrame(() => { updateNavigation(); scrollPending = false; });
  }
}, { passive: true });
render();
updateNavigation();
