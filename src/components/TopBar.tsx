// TopBar: top control row (render params + canvas size + backend/assist) and the engine progress bar.
import { InputNumber, Select, Button, Progress } from "@douyinfe/semi-ui";
import type { TopBarProps } from "../types";

export default function TopBar(props: TopBarProps) {
  const {
    stopAt,
    setStopAt,
    canvasWidth,
    setCanvasWidth,
    canvasHeight,
    setCanvasHeight,
    stickerMode,
    setStickerMode,
    bgColor,
    setBgColor,
    backend,
    setBackend,
    assistMode,
    setAssistMode,
    backendText,
    progress,
    running,
    canStart,
    onStart,
    onImportJson,
  } = props;

  const pct = progress.total > 0 ? Math.min(100, (progress.n / progress.total) * 100) : 0;
  const label =
    progress.n + " / " + progress.total + " 形状 · RMS " + progress.rms.toFixed(2) + " · " + pct.toFixed(0) + "%";

  return (
    <>
      <div className="controls">
        <label className="field">
          目标形状数
          <InputNumber
            value={stopAt}
            onChange={(v) => setStopAt(Number(v))}
            min={50}
            max={8000}
            step={50}
            style={{ width: 84 }}
          />
        </label>
        <label className="field">
          画布宽
          <InputNumber
            value={canvasWidth}
            onChange={(v) => setCanvasWidth(Number(v))}
            min={200}
            max={4096}
            step={50}
            style={{ width: 84 }}
          />
        </label>
        <label className="field">
          画布高
          <InputNumber
            value={canvasHeight}
            onChange={(v) => setCanvasHeight(Number(v))}
            min={200}
            max={4096}
            step={50}
            style={{ width: 84 }}
          />
        </label>
        <label className="field">
          渲染模式
          <Select
            value={stickerMode}
            onChange={(v) => setStickerMode(v as string)}
            style={{ width: 200 }}
            optionList={[
              { value: "sticker", label: "贴纸模式（保留透明度）" },
              { value: "default", label: "默认模式" },
            ]}
          />
        </label>
        <label
          className={"field" + (stickerMode === "sticker" ? " is-disabled" : "")}
          title="默认模式下画布缓冲区（图片四周到 W×H）的填充色；贴纸模式保留透明"
        >
          背景色
          <input
            type="color"
            value={bgColor}
            onChange={(e) => setBgColor(e.target.value)}
            disabled={stickerMode === "sticker"}
          />
        </label>
        <label className="field">
          计算后端
          <Select
            value={backend}
            onChange={(v) => setBackend(v as string)}
            style={{ width: 140 }}
            optionList={[
              { value: "gpu", label: "GPU（默认）" },
              { value: "cpu", label: "CPU" },
              { value: "auto", label: "自动" },
            ]}
          />
        </label>
        <label
          className="field"
          title="用图像模型协助渲染：压平色块 + 底图打底 + 显著性引导，更少图层、更高精细度"
        >
          模型协助
          <Select
            value={assistMode}
            onChange={(v) => setAssistMode(v as string)}
            style={{ width: 200 }}
            optionList={[
              { value: "off", label: "关闭" },
              { value: "on", label: "开启（减少图层 · 增精细度）" },
            ]}
          />
        </label>
        <span className="backend">{backendText}</span>
        <span className="spacer" />
        <Button theme="light" type="tertiary" onClick={onImportJson} disabled={running}>
          导入 JSON
        </Button>
        <Button theme="solid" type="primary" onClick={onStart} disabled={!canStart}>
          开始渲染
        </Button>
      </div>
      <div className="progress-wrap">
        <Progress percent={pct} stroke="var(--accent)" showInfo={false} aria-label="渲染进度" />
        <div className="progress-label">{label}</div>
      </div>
    </>
  );
}
