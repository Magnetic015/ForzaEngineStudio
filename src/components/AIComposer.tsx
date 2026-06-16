// AI 预处理输入区：Semi AIChatInput（基于 tiptap 的富文本输入）。底部配置区
// （renderConfigureArea）放一个「模型选择器」，展开后含 API Key 输入 + 模型列表；
// 发送时由父级取走纯文本。
import { AIChatInput, Popover, Input, Button } from "@douyinfe/semi-ui";
import { useRef, useState } from "react";
import { MODELS } from "../types";
import type { AIComposerProps } from "../types";

// Combined model selector + API key. One trigger expands to a panel with the key
// input on top and the model list below. The key uses a LOCAL draft committed on
// blur/close, so typing never re-renders the AIChatInput. The trigger shows the
// model once a key exists, otherwise prompts 「请选择模型」.
function ModelConfigure({
  apiKey,
  onApiKeyChange,
  model,
  onSelectModel,
}: {
  apiKey: string;
  onApiKeyChange: (v: string) => void;
  model: string;
  onSelectModel: (m: string) => void;
}) {
  const [visible, setVisible] = useState(false);
  const [draft, setDraft] = useState(apiKey);
  const commit = () => onApiKeyChange(draft);
  const hasKey = apiKey.trim().length > 0;
  const hasModel = model.trim().length > 0;

  const content = (
    <div className="model-pop" onMouseDown={(e) => e.stopPropagation()} onClick={(e) => e.stopPropagation()}>
      <label className="field">
        API Key
        <Input mode="password" value={draft} onChange={(v) => setDraft(v)} onBlur={commit} placeholder="sk-..." />
      </label>
      <div className="model-pop-div" />
      <div className="model-pop-head">模型</div>
      <div className="model-list">
        {MODELS.map((m) => (
          <button
            type="button"
            key={m}
            className={"model-item" + (hasKey && m === model ? " is-selected" : "")}
            onClick={() => onSelectModel(m)}
          >
            <span className="tick">✓</span>
            <span className="popname">{m}</span>
          </button>
        ))}
      </div>
    </div>
  );

  return (
    <Popover
      trigger="custom"
      visible={visible}
      onVisibleChange={(v) => {
        setVisible(v);
        if (!v) commit();
      }}
      onClickOutSide={() => {
        setVisible(false);
        commit();
      }}
      position="top"
      content={content}
    >
      <Button
        theme="borderless"
        size="small"
        type={hasKey && hasModel ? "tertiary" : "warning"}
        onClick={() => setVisible((v) => !v)}
        aria-haspopup="true"
        aria-expanded={visible}
      >
        {!hasKey ? "请输入key" : !hasModel ? "请选择模型" : model} ▾
      </Button>
    </Popover>
  );
}

export default function AIComposer(props: AIComposerProps) {
  const { onSend, aiRunning, sendBlocked, lastPrompt, apiKey, setApiKey, model, onSelectModel } = props;
  // Semi class component; its instance exposes getEditor()/setContent()… (typed loosely).
  const ref = useRef<any>(null);
  const [hasContent, setHasContent] = useState(false);

  const editorText = (): string => ref.current?.getEditor?.()?.getText?.() ?? "";

  // AIChatInput's container onClick refocuses the editor on any non-editor click.
  // React portal events bubble through the React tree, so a click in the model/key
  // popover reaches that handler and steals focus — stop propagation to prevent it.
  const renderConfigureArea = () => (
    <div
      style={{ display: "flex", alignItems: "center", gap: 8 }}
      onMouseDown={(e) => e.stopPropagation()}
      onClick={(e) => e.stopPropagation()}
    >
      <ModelConfigure apiKey={apiKey} onApiKeyChange={setApiKey} model={model} onSelectModel={onSelectModel} />
    </div>
  );

  return (
    <>
      {lastPrompt && (
        <div className="last-prompt">
          <span key={lastPrompt} className="last-prompt-text" title={lastPrompt}>
            {lastPrompt}
          </span>
        </div>
      )}
      <AIChatInput
        ref={ref}
        style={{ width: "100%" }}
        placeholder="处理指令，如：转成卡通风格"
        sendHotKey="enter"
        generating={aiRunning}
        canSend={hasContent && !sendBlocked}
        showUploadButton={false}
        renderConfigureArea={renderConfigureArea}
        onContentChange={() => setHasContent(!!editorText().trim())}
        onMessageSend={() => onSend(editorText())}
        onStopGenerate={() => {}}
      />
    </>
  );
}
