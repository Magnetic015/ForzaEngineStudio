use std::io::{BufRead, BufReader};
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::{Arc, Mutex};

use base64::Engine as _;
use tauri::{AppHandle, Emitter};

/// Tracks the currently running generation sidecar so `stop_generation` can kill
/// it. Holds the OS pid (cleared by the reaper when the process ends). Only one
/// render runs at a time (the frontend gates on `running`).
#[derive(Default)]
struct EngineState {
    pid: Arc<Mutex<Option<u32>>>,
}

/// Locate the vendored `python/` dir (sibling of `src-tauri/`).
/// NOTE: uses CARGO_MANIFEST_DIR — valid for `tauri dev`. For a bundled release
/// this should switch to a resource path; out of scope for the dev MVP.
fn project_python_dir() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .map(|p| p.join("python"))
        .unwrap_or_else(|| PathBuf::from("python"))
}

fn venv_python(py_dir: &Path) -> PathBuf {
    if cfg!(windows) {
        py_dir.join(".venv").join("Scripts").join("python.exe")
    } else {
        py_dir.join(".venv").join("bin").join("python")
    }
}

/// Start a generation run: spawn the Python engine sidecar and stream its
/// line-JSON events to the frontend as `engine-event`. Returns immediately;
/// progress/preview/done arrive asynchronously via events.
#[tauri::command]
fn start_generation(
    app: AppHandle,
    state: tauri::State<'_, EngineState>,
    image: String,
    stop_at: u32,
    canvas_width: u32,
    canvas_height: u32,
    sticker: bool,
    backend: String,
    assist: bool,
    bg_color: String,
    generation: u64,
) -> Result<(), String> {
    let py_dir = project_python_dir();
    let script = py_dir.join("sidecar.py");
    if !script.exists() {
        return Err(format!("sidecar not found: {}", script.display()));
    }
    let venv = venv_python(&py_dir);
    let python = if venv.exists() { venv } else { PathBuf::from("python") };

    let mut cmd = Command::new(&python);
    cmd.arg(&script)
        .arg("--image").arg(&image)
        .arg("--stop-at").arg(stop_at.to_string())
        .arg("--canvas-width").arg(canvas_width.to_string())
        .arg("--canvas-height").arg(canvas_height.to_string())
        .arg("--bg-color").arg(&bg_color)
        .arg("--backend").arg(&backend)
        .current_dir(&py_dir)
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    if sticker {
        cmd.arg("--sticker");
    }
    // Model-assist: render-optimize + hybrid base + saliency guidance (fewer
    // layers, more detail). The sidecar builds the assist inputs locally.
    if assist {
        cmd.arg("--assist");
    }
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        const CREATE_NO_WINDOW: u32 = 0x0800_0000;
        cmd.creation_flags(CREATE_NO_WINDOW);
    }
    // Own process group so stop_generation can signal the whole render tree — the
    // engine's ProcessPoolExecutor workers are children of the sidecar.
    #[cfg(unix)]
    {
        use std::os::unix::process::CommandExt;
        cmd.process_group(0);
    }

    let mut child = cmd
        .spawn()
        .map_err(|e| format!("failed to start sidecar ({}): {e}", python.display()))?;

    let stdout = child.stdout.take().ok_or("no stdout pipe")?;
    let stderr = child.stderr.take().ok_or("no stderr pipe")?;

    // stdout: one JSON event per line -> tag with the caller's `generation` and
    // forward to the frontend. The frontend assigns the gen before invoking, so
    // even events emitted during startup carry the gen it is already filtering on.
    let app_out = app.clone();
    std::thread::spawn(move || {
        let reader = BufReader::new(stdout);
        for line in reader.lines() {
            match line {
                Ok(l) if !l.trim().is_empty() => match serde_json::from_str::<serde_json::Value>(&l) {
                    Ok(mut v) => {
                        if let Some(obj) = v.as_object_mut() {
                            obj.insert("gen".into(), serde_json::json!(generation));
                        }
                        let _ = app_out.emit("engine-event", v);
                    }
                    Err(_) => {
                        let _ = app_out
                            .emit("engine-event", serde_json::json!({"type":"log","message": l}));
                    }
                },
                _ => {}
            }
        }
    });

    // stderr: surface Python tracebacks/warnings as log events.
    let app_err = app.clone();
    std::thread::spawn(move || {
        let reader = BufReader::new(stderr);
        for line in reader.lines().flatten() {
            if !line.trim().is_empty() {
                let _ = app_err
                    .emit("engine-event", serde_json::json!({"type":"log","message": line}));
            }
        }
    });

    // Record the pid so `stop_generation` can terminate this run on request.
    *state.pid.lock().unwrap() = Some(child.id());

    // Reap the child; clear our pid slot and report abnormal exit — this covers
    // both a crash and a user-initiated stop — so the UI can unstick itself. The
    // exit carries `gen` so the frontend ignores a stopped render's late exit.
    let app_wait = app.clone();
    let pid_slot = state.pid.clone();
    std::thread::spawn(move || {
        let my_pid = child.id();
        let status = child.wait();
        if let Ok(mut g) = pid_slot.lock() {
            if *g == Some(my_pid) {
                *g = None;
            }
        }
        if let Ok(status) = status {
            if !status.success() {
                let _ = app_wait.emit(
                    "engine-event",
                    serde_json::json!({"type":"exit","code": status.code(),"gen": generation}),
                );
            }
        }
    });

    Ok(())
}

/// Stop the in-flight generation: terminate the sidecar process tree by pid.
/// The reaper thread observes the exit and clears the pid slot; the frontend
/// resets its own UI optimistically, so the resulting `exit` event is ignored.
#[tauri::command]
fn stop_generation(state: tauri::State<'_, EngineState>) -> Result<(), String> {
    let pid = *state.pid.lock().unwrap();
    match pid {
        Some(pid) => kill_pid(pid),
        None => Ok(()),
    }
}

/// Best-effort terminate a process (and its children) by pid, cross-platform.
fn kill_pid(pid: u32) -> Result<(), String> {
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        const CREATE_NO_WINDOW: u32 = 0x0800_0000;
        let status = Command::new("taskkill")
            .args(["/PID", &pid.to_string(), "/T", "/F"])
            .creation_flags(CREATE_NO_WINDOW)
            .status()
            .map_err(|e| format!("taskkill failed to run: {e}"))?;
        if status.success() {
            Ok(())
        } else {
            Err(format!("taskkill exited with {:?}", status.code()))
        }
    }
    #[cfg(not(windows))]
    {
        // The sidecar leads its own process group (see start_generation), so a
        // negative pid signals the whole group — sidecar + ProcessPoolExecutor
        // workers. SIGKILL matches the Windows /F force-terminate semantics.
        let status = Command::new("kill")
            .arg("-KILL")
            .arg(format!("-{pid}"))
            .status()
            .map_err(|e| format!("kill failed to run: {e}"))?;
        if status.success() {
            Ok(())
        } else {
            Err(format!("kill exited with {:?}", status.code()))
        }
    }
}

/// Run the AI image-edit sidecar and return the edited image path.
///
/// The sidecar call is a synchronous process spawn + wait that can take 10–40s
/// (the third-party model does the work). Run it on the blocking pool via
/// `spawn_blocking` so an `async` command frees the async runtime — and thus the
/// UI — instead of stalling on the main thread while the model runs.
#[tauri::command]
async fn ai_process_image(
    image: String,
    api_key: String,
    model: String,
    prompt: String,
) -> Result<String, String> {
    tauri::async_runtime::spawn_blocking(move || -> Result<String, String> {
        let py_dir = project_python_dir();
        let script = py_dir.join("image_process.py");
        if !script.exists() {
            return Err(format!("image_process.py not found: {}", script.display()));
        }
        let venv = venv_python(&py_dir);
        let python = if venv.exists() { venv } else { PathBuf::from("python") };

        let mut cmd = Command::new(&python);
        cmd.arg(&script)
            .arg("--image").arg(&image)
            .arg("--api-key").arg(&api_key)
            .arg("--model").arg(&model)
            .arg("--prompt").arg(&prompt)
            .current_dir(&py_dir)
            .stdout(Stdio::piped())
            .stderr(Stdio::piped());
        #[cfg(windows)]
        {
            use std::os::windows::process::CommandExt;
            cmd.creation_flags(0x0800_0000);
        }

        let output = cmd.output().map_err(|e| format!("failed to start sidecar: {e}"))?;
        let stdout = String::from_utf8_lossy(&output.stdout);
        for line in stdout.lines().rev() {
            let line = line.trim();
            if line.is_empty() {
                continue;
            }
            if let Ok(v) = serde_json::from_str::<serde_json::Value>(line) {
                match v.get("type").and_then(|t| t.as_str()) {
                    Some("done") => {
                        return v
                            .get("path")
                            .and_then(|p| p.as_str())
                            .map(|s| s.to_string())
                            .ok_or_else(|| "sidecar done without path".to_string());
                    }
                    Some("error") => {
                        return Err(v
                            .get("message")
                            .and_then(|m| m.as_str())
                            .unwrap_or("unknown error")
                            .to_string());
                    }
                    _ => {}
                }
            }
        }
        Err(format!(
            "no result from sidecar; stderr: {}",
            String::from_utf8_lossy(&output.stderr).trim()
        ))
    })
    .await
    .map_err(|e| format!("AI task failed to run: {e}"))?
}

/// Sniff a common image MIME type from a file's magic bytes.
fn sniff_image_mime(b: &[u8]) -> &'static str {
    if b.len() >= 8 && b[..8] == [0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A] {
        "image/png"
    } else if b.len() >= 3 && b[..3] == [0xFF, 0xD8, 0xFF] {
        "image/jpeg"
    } else if b.len() >= 6 && (&b[..6] == b"GIF87a" || &b[..6] == b"GIF89a") {
        "image/gif"
    } else if b.len() >= 12 && &b[..4] == b"RIFF" && &b[8..12] == b"WEBP" {
        "image/webp"
    } else if b.len() >= 2 && &b[..2] == b"BM" {
        "image/bmp"
    } else {
        "application/octet-stream"
    }
}

/// Read a local image file and return it as a `data:` URL so the webview can
/// display it in an <img> tag (used for the source / AI-processed thumbnails).
/// Avoids needing the asset protocol; runs on the blocking pool since a large
/// source image read + base64 can take a moment.
#[tauri::command]
async fn read_image_data_url(path: String) -> Result<String, String> {
    tauri::async_runtime::spawn_blocking(move || -> Result<String, String> {
        let bytes = std::fs::read(&path).map_err(|e| format!("read failed: {e}"))?;
        let mime = sniff_image_mime(&bytes);
        let b64 = base64::engine::general_purpose::STANDARD.encode(&bytes);
        Ok(format!("data:{mime};base64,{b64}"))
    })
    .await
    .map_err(|e| format!("read task failed: {e}"))?
}

/// Import an existing FD6 shape JSON: render it to a PNG and return a data URL
/// for the preview pane. Runs the `render_json.py` helper on the blocking pool.
#[tauri::command]
async fn import_json(json_path: String) -> Result<String, String> {
    tauri::async_runtime::spawn_blocking(move || -> Result<String, String> {
        let py_dir = project_python_dir();
        let script = py_dir.join("render_json.py");
        if !script.exists() {
            return Err(format!("render_json.py not found: {}", script.display()));
        }
        let venv = venv_python(&py_dir);
        let python = if venv.exists() { venv } else { PathBuf::from("python") };

        let mut cmd = Command::new(&python);
        cmd.arg(&script)
            .arg("--json").arg(&json_path)
            .current_dir(&py_dir)
            .stdout(Stdio::piped())
            .stderr(Stdio::piped());
        #[cfg(windows)]
        {
            use std::os::windows::process::CommandExt;
            cmd.creation_flags(0x0800_0000);
        }

        let output = cmd.output().map_err(|e| format!("failed to start renderer: {e}"))?;
        let stdout = String::from_utf8_lossy(&output.stdout);
        for line in stdout.lines().rev() {
            let line = line.trim();
            if line.is_empty() {
                continue;
            }
            if let Ok(v) = serde_json::from_str::<serde_json::Value>(line) {
                match v.get("type").and_then(|t| t.as_str()) {
                    Some("done") => {
                        return v
                            .get("png")
                            .and_then(|p| p.as_str())
                            .map(|s| format!("data:image/png;base64,{s}"))
                            .ok_or_else(|| "renderer done without png".to_string());
                    }
                    Some("error") => {
                        return Err(v
                            .get("message")
                            .and_then(|m| m.as_str())
                            .unwrap_or("unknown error")
                            .to_string());
                    }
                    _ => {}
                }
            }
        }
        Err(format!(
            "no result from renderer; stderr: {}",
            String::from_utf8_lossy(&output.stderr).trim()
        ))
    })
    .await
    .map_err(|e| format!("import task failed: {e}"))?
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_opener::init())
        .plugin(tauri_plugin_dialog::init())
        .manage(EngineState::default())
        .invoke_handler(tauri::generate_handler![start_generation, stop_generation, ai_process_image, read_image_data_url, import_json])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
