import { useEffect, useRef } from "react";
import { isTauri, listenEngine, type EngineEvent } from "../api/tauri";

// Subscribe once to the `engine-event` stream and always dispatch to the latest
// handler (kept in a ref so the listener never needs re-subscribing). No-op
// outside the Tauri webview.
export function useEngineEvents(onEvent: (e: EngineEvent) => void) {
  const ref = useRef(onEvent);
  ref.current = onEvent;

  useEffect(() => {
    if (!isTauri) return;
    let alive = true;
    let unlisten: (() => void) | undefined;
    listenEngine((e) => ref.current(e)).then((un) => {
      if (alive) unlisten = un;
      else un();
    });
    return () => {
      alive = false;
      unlisten?.();
    };
  }, []);
}
