// Model selector: click-to-open Popover with an API-key field + selectable model list and 1–3/Esc keyboard shortcuts.
import { Popover, Button, Input } from "@douyinfe/semi-ui";
import { MODELS } from "../types";
import { useState, useEffect } from "react";
import type { ModelSelectorProps } from "../types";

export default function ModelSelector({ apiKey, setApiKey, model, onSelectModel }: ModelSelectorProps) {
  const [visible, setVisible] = useState(false);

  useEffect(() => {
    if (!visible) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        setVisible(false);
        return;
      }
      const idx = ["1", "2", "3"].indexOf(e.key);
      if (idx >= 0 && MODELS[idx]) {
        onSelectModel(MODELS[idx]);
        setVisible(false);
      }
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [visible, onSelectModel]);

  const content = (
    <div className="model-pop">
      <label className="field">
        API Key
        <Input mode="password" value={apiKey} onChange={(v) => setApiKey(v)} placeholder="sk-..." />
      </label>
      <div className="model-pop-div" />
      <div className="model-pop-head">模型</div>
      <div className="model-list">
        {MODELS.map((m, i) => (
          <button
            type="button"
            key={m}
            className={"model-item" + (m === model ? " is-selected" : "")}
            onClick={() => {
              onSelectModel(m);
              setVisible(false);
            }}
          >
            <span className="tick">✓</span>
            <span className="popname">{m}</span>
            <span className="kbd">{i + 1}</span>
          </button>
        ))}
      </div>
    </div>
  );

  return (
    <Popover
      trigger="custom"
      visible={visible}
      onVisibleChange={setVisible}
      onClickOutSide={() => setVisible(false)}
      position="top"
      content={content}
    >
      <Button
        theme="borderless"
        size="small"
        onClick={() => setVisible((v) => !v)}
        aria-haspopup="true"
        aria-expanded={visible}
      >
        {model} ▾
      </Button>
    </Popover>
  );
}
