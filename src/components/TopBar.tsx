// TopBar: top control row (render params + canvas size + backend/assist) and the engine progress bar.
import { InputNumber, Select, Button, Progress, ColorPicker, Tooltip, Slider } from "@douyinfe/semi-ui";
import { IconHelpCircle } from "@douyinfe/semi-icons";
import type { TopBarProps } from "../types";

export default function TopBar(props: TopBarProps) {
  const {
    stopAt,
    setStopAt,
    quality,
    setQuality,
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
    progress,
    running,
    canStart,
    onStart,
    onStop,
    onImportJson,
    onResetPreview,
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
            min={1000}
            max={3000}
            step={50}
            style={{ width: 84 }}
          />
        </label>
        <label className="field">
          <span className="field-label">
            渲染质量
            <Tooltip content="更高质量=更大搜索预算+更精细的采样/透明度/重拟合，渲染更慢">
              <IconHelpCircle size="small" className="field-help" />
            </Tooltip>
          </span>
          <Slider
            value={quality}
            onChange={(v) => setQuality(Number(v))}
            min={1}
            max={4}
            step={1}
            marks={{ 1: "草稿", 2: "标准", 3: "精细", 4: "极致" }}
            style={{ width: 160 }}
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
          <span className="field-label">
            渲染模式
            <Tooltip content="贴纸模式保留透明度">
              <IconHelpCircle size="small" className="field-help" />
            </Tooltip>
          </span>
          <Select
            value={stickerMode}
            onChange={(v) => setStickerMode(v as string)}
            style={{ width: 110 }}
            optionList={[
              { value: "sticker", label: "贴纸模式" },
              { value: "default", label: "默认模式" },
            ]}
          />
        </label>
        <label
          className={"field" + (stickerMode === "sticker" ? " is-disabled" : "")}
          title="默认模式下画布缓冲区（图片四周到 W×H）的填充色；贴纸模式保留透明"
        >
          背景色
          <ColorPicker
            alpha={false}
            usePopover
            value={ColorPicker.colorStringToValue(bgColor)}
            onChange={(v: any) => {
              const hex: string = v?.hex || "";
              setBgColor((hex.startsWith("#") ? hex : "#" + hex).slice(0, 7));
            }}
          />
        </label>
        <label className="field">
          计算后端
          <Select
            value={backend}
            onChange={(v) => setBackend(v as string)}
            style={{ width: 96 }}
            optionList={[
              { value: "gpu", label: "GPU" },
              { value: "cpu", label: "CPU" },
              { value: "auto", label: "自动" },
            ]}
          />
        </label>
        <label className="field">
          <span className="field-label">
            模型协助
            <Tooltip content="开启时减少图层，增加精度">
              <IconHelpCircle size="small" className="field-help" />
            </Tooltip>
          </span>
          <Select
            value={assistMode}
            onChange={(v) => setAssistMode(v as string)}
            style={{ width: 96 }}
            optionList={[
              { value: "off", label: "关闭" },
              { value: "on", label: "开启" },
            ]}
          />
        </label>
        <span className="spacer" />
        <div className="btn-group">
          <Button theme="light" type="tertiary" onClick={onImportJson} disabled={running}>
            导入 JSON
          </Button>
          <Button theme="light" type="tertiary" onClick={onResetPreview} disabled={running}>
            重置
          </Button>
          {running ? (
            <Button theme="solid" type="danger" onClick={onStop}>
              终止渲染
            </Button>
          ) : (
            <Button theme="solid" type="primary" onClick={onStart} disabled={!canStart}>
              开始渲染
            </Button>
          )}
        </div>
      </div>
      <div className="progress-wrap">
        <Progress percent={pct} stroke="var(--accent)" showInfo={false} aria-label="渲染进度" />
        <div className="progress-label">{label}</div>
      </div>
    </>
  );
}
