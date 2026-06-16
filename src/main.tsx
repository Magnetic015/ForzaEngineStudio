import { createRoot } from "react-dom/client";
import "./styles.css";
import App from "./App";

// Semi auto-imports each used component's CSS, and its base palette (light +
// dark tokens on <body>) ships via the package entry. This app is dark-only;
// styles.css is imported after so its orange-accent overrides win.
document.body.setAttribute("theme-mode", "dark");

createRoot(document.getElementById("root")!).render(<App />);
