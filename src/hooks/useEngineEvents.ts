import { useEffect, useRef } from "react";
import { isTauri, listenEngine, listenInject, type EngineEvent, type InjectEvent } from "../api/tauri";

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

// Same pattern as useEngineEvents, but for the separate `inject-event` stream.
export function useInjectEvents(onEvent: (e: InjectEvent) => void) {
  const ref = useRef(onEvent);
  ref.current = onEvent;

  useEffect(() => {
    if (!isTauri) return;
    let alive = true;
    let unlisten: (() => void) | undefined;
    listenInject((e) => ref.current(e)).then((un) => {
      if (alive) unlisten = un;
      else un();
    });
    return () => {
      alive = false;
      unlisten?.();
    };
  }, []);
}
