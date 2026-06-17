// Modal that crops the selected target image with Semi's Cropper. On "保存裁剪"
// it reads the cropped canvas as a PNG data URL and hands it back to the caller,
// which persists it and adds it to the candidate list.
import { useCallback, useEffect, useRef, useState } from "react";
import { Modal, Cropper, Slider } from "@douyinfe/semi-ui";
import type { CropModalProps } from "../types";

export default function CropModal({ visible, src, saving, onCancel, onSave }: CropModalProps) {
  const ref = useRef<Cropper>(null);
  const [rotate, setRotate] = useState(0);
  const [zoom, setZoom] = useState(1);

  // Reset transforms each time the modal opens so a new crop starts clean.
  useEffect(() => {
    if (visible) {
      setRotate(0);
      setZoom(1);
    }
  }, [visible]);

  const handleOk = useCallback(() => {
    const canvas = ref.current?.getCropperCanvas();
    if (!canvas) return;
    onSave(canvas.toDataURL("image/png"));
  }, [onSave]);

  return (
    <Modal
      title="裁剪图片"
      visible={visible}
      onCancel={onCancel}
      onOk={handleOk}
      okText="保存裁剪"
      cancelText="取消"
      confirmLoading={saving}
      width={720}
      centered
    >
      <div className="crop-stage">
        {src ? (
          <Cropper ref={ref} src={src} style={{ width: "100%", height: 380 }} rotate={rotate} zoom={zoom} onZoomChange={setZoom} />
        ) : (
          <div className="crop-empty">没有可裁剪的图片</div>
        )}
      </div>
      <div className="crop-controls">
        <label className="crop-slider">
          <span>缩放</span>
          <Slider value={zoom} min={0.1} max={3} step={0.1} onChange={(v) => setZoom(Number(v))} />
        </label>
        <label className="crop-slider">
          <span>旋转</span>
          <Slider value={rotate} min={-180} max={180} step={1} onChange={(v) => setRotate(Number(v))} />
        </label>
      </div>
    </Modal>
  );
}
