// Vertical thumbnail strip of candidate images (原图 + AI results); empty-state "+" opens the picker.
import type { CandidateStripProps } from "../types";

export default function CandidateStrip({ candidates, selectedIndex, onSelect, onPick }: CandidateStripProps) {
  return (
    <div className="candidates" aria-label="候选图：原图与 AI 处理结果">
      {candidates.length === 0 ? (
        <button type="button" className="cand cand-empty" title="点击选择本地图片" onClick={onPick}>
          <span className="cand-thumb">+</span>
        </button>
      ) : (
        candidates.map((c, i) => (
          <button
            type="button"
            key={i}
            className={"cand" + (i === selectedIndex ? " is-selected" : "")}
            title={c.label}
            onClick={() => onSelect(i)}
          >
            <span className="cand-thumb">
              <img src={c.src} alt={c.label} />
            </span>
          </button>
        ))
      )}
    </div>
  );
}
