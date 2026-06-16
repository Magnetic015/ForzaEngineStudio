import { useCallback, useRef, useState } from "react";

// Draggable splitter state: the left panel width as a % of the `.body` width
// (the right side flexes to fill). Each side is clamped to a minimum of 30%, so
// the % survives window resizes. Attach `bodyRef` to `.body`, apply
// `flex: 0 0 {leftPct}%` to `.left`, and wire `onMouseDown` on the splitter.
export function useSplitter(initialPct = 50) {
  const [leftPct, setLeftPct] = useState(initialPct);
  const [dragging, setDragging] = useState(false);
  const bodyRef = useRef<HTMLDivElement | null>(null);

  const onMouseDown = useCallback((e: React.MouseEvent) => {
    e.preventDefault();
    const body = bodyRef.current;
    if (!body) return;
    setDragging(true);
    document.body.style.userSelect = "none";

    const move = (ev: MouseEvent) => {
      const rect = body.getBoundingClientRect();
      if (rect.width <= 0) return;
      let pct = ((ev.clientX - rect.left) / rect.width) * 100;
      pct = Math.max(30, Math.min(70, pct));
      setLeftPct(pct);
    };
    const up = () => {
      setDragging(false);
      document.body.style.userSelect = "";
      document.removeEventListener("mousemove", move);
      document.removeEventListener("mouseup", up);
    };
    document.addEventListener("mousemove", move);
    document.addEventListener("mouseup", up);
  }, []);

  return { leftPct, dragging, bodyRef, onMouseDown };
}
