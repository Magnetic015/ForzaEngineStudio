// AI 预处理输入区：处理指令文本框（Enter 发送 / Shift+Enter 换行）+ 模型选择行。
import { TextArea, Button } from "@douyinfe/semi-ui";
import type { AIComposerProps } from "../types";
import ModelSelector from "./ModelSelector";

export default function AIComposer(props: AIComposerProps) {
  const { prompt, setPrompt, onSend, sendDisabled, apiKey, setApiKey, model, onSelectModel } = props;

  const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      if (!sendDisabled) onSend();
    }
  };

  return (
    <>
      <div className="composer">
        <TextArea
          value={prompt}
          onChange={(v) => setPrompt(v)}
          placeholder="处理指令，如：转成卡通风格"
          rows={3}
          style={{ height: "100%" }}
          textareaStyle={{ height: "100%", resize: "none", paddingRight: 40 }}
          onKeyDown={handleKeyDown}
        />
        <Button
          className="entbtn"
          theme="solid"
          type="primary"
          size="small"
          onClick={onSend}
          disabled={sendDisabled}
          title="发送（Enter）"
          aria-label="发送（AI 处理图像）"
        >
          ↵
        </Button>
      </div>
      <div className="model-row">
        <ModelSelector apiKey={apiKey} setApiKey={setApiKey} model={model} onSelectModel={onSelectModel} />
      </div>
    </>
  );
}
