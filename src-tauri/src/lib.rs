use std::io::{BufRead, BufReader};
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::{Arc, Mutex};

use base64::Engine as _;
use tauri::{AppHandle, Emitter, Manager};

/// Tracks the currently running generation sidecar so `stop_generation` can kill
/// it. Holds the OS pid (cleared by the reaper when the process ends). Only one
/// render runs at a time (the frontend gates on `running`).
#[derive(Default)]
struct EngineState {
    pid: Arc<Mutex<Option<u32>>>,
}

/// Locate the vendored `python/` dir (sibling of `src-tauri/`) for a DEV run.
/// Uses CARGO_MANIFEST_DIR — valid under `tauri dev`. A packaged release instead
/// spawns the frozen `fes-engine` exe shipped as a resource (see `engine_command`).
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

/// Resolve the frozen sidecar exe shipped as a Tauri resource, if present.
/// Returns `Some(path)` only for a packaged/installed app (where `tauri build`
/// copied `pyengine/fes-engine/` under the resource dir); `None` under
/// `tauri dev` or a bare `cargo run`, so callers fall back to the dev venv.
fn bundled_engine_exe(app: &AppHandle) -> Option<PathBuf> {
    let exe_name = if cfg!(windows) { "fes-engine.exe" } else { "fes-engine" };
    let exe = app
        .path()
        .resource_dir()
        .ok()?
        .join("pyengine")
        .join("fes-engine")
        .join(exe_name);
    exe.exists().then_some(exe)
}

/// Build the base `Command` for an engine tool, transparently handling both a
/// packaged release (the frozen `fes-engine` exe + a `subcommand` verb) and a
/// dev run (the vendored `python/.venv` + the loose `dev_script`). The caller
/// appends the tool-specific flags and the stdio/`creation_flags` config.
/// `subcommand` is the dispatcher verb ("generate" | "ai" | "render-json");
/// `dev_script` is the matching loose script name.
fn engine_command(app: &AppHandle, subcommand: &str, dev_script: &str) -> Result<Command, String> {
    if let Some(exe) = bundled_engine_exe(app) {
        let mut cmd = Command::new(&exe);
        cmd.arg(subcommand);
        // Anchor CWD to the frozen exe's own dir (symmetry with the dev branch,
        // which uses python/). Every path we pass is absolute today, so this is
        // defensive: it keeps a future relative path from silently breaking in a
        // bundled install, where the inherited CWD is the shortcut's target.
        if let Some(dir) = exe.parent() {
            cmd.current_dir(dir);
        }
        return Ok(cmd);
    }
    let py_dir = project_python_dir();
    let script = py_dir.join(dev_script);
    if !script.exists() {
        return Err(format!("{dev_script} not found: {}", script.display()));
    }
    let venv = venv_python(&py_dir);
    let python = if venv.exists() { venv } else { PathBuf::from("python") };
    let mut cmd = Command::new(python);
    cmd.arg(&script).current_dir(&py_dir);
    Ok(cmd)
}

/// App-data output dirs for app-generated assets: `<app_data>/images` and
/// `<app_data>/data`, created on demand. Centralizes where AI / crop images and
/// engine JSON land instead of scattering them next to each source image.
fn output_dirs(app: &AppHandle) -> Result<(PathBuf, PathBuf), String> {
    let base = app
        .path()
        .app_data_dir()
        .map_err(|e| format!("cannot resolve app data dir: {e}"))?;
    let images = base.join("images");
    let data = base.join("data");
    std::fs::create_dir_all(&images).map_err(|e| format!("cannot create images dir: {e}"))?;
    std::fs::create_dir_all(&data).map_err(|e| format!("cannot create data dir: {e}"))?;
    Ok((images, data))
}

/// Milliseconds since the Unix epoch — the timestamp suffix for generated files.
fn timestamp_millis() -> u128 {
    use std::time::{SystemTime, UNIX_EPOCH};
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_millis())
        .unwrap_or(0)
}

/// Build `<source-stem>_<timestamp>.<ext>` — the rename scheme for app-generated
/// images / JSON derived from a source file.
fn timestamped_name(source: &str, ext: &str) -> String {
    let stem = Path::new(source)
        .file_stem()
        .and_then(|s| s.to_str())
        .filter(|s| !s.is_empty())
        .unwrap_or("image");
    format!("{stem}_{}.{ext}", timestamp_millis())
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
    quality: u32,
    bg_color: String,
    generation: u64,
) -> Result<(), String> {
    // Generated shape JSON goes to the app-data data dir, named <source>_<ts>.json.
    let (_, data_dir) = output_dirs(&app)?;
    let out_json = data_dir.join(timestamped_name(&image, "json"));

    let mut cmd = engine_command(&app, "generate", "sidecar.py")?;
    cmd.arg("--image").arg(&image)
        .arg("--stop-at").arg(stop_at.to_string())
        .arg("--canvas-width").arg(canvas_width.to_string())
        .arg("--canvas-height").arg(canvas_height.to_string())
        .arg("--bg-color").arg(&bg_color)
        .arg("--backend").arg(&backend)
        .arg("--quality").arg(quality.to_string())
        .arg("--out").arg(&out_json)
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

    let mut child = cmd
        .spawn()
        .map_err(|e| format!("failed to start engine sidecar: {e}"))?;

    // Record the pid immediately — before taking pipes or spawning reader threads
    // — so the exit hook (and `stop_generation`) can always find a just-started
    // sidecar. Recording it later leaves a window where app exit races startup and
    // orphans the render, which is exactly the GPU leak the exit hook prevents.
    *state.pid.lock().unwrap() = Some(child.id());

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

/// Terminate the sidecar process tree by pid. This app targets Windows; the
/// non-Windows arm is a minimal stub so the crate still builds elsewhere.
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
        // exit 128 = "process not found": the target is already gone, which is
        // exactly the outcome stop wants (covers the race where the render finishes
        // naturally just before stop is processed), so treat it as success.
        if status.success() || status.code() == Some(128) {
            Ok(())
        } else {
            Err(format!("taskkill exited with {:?}", status.code()))
        }
    }
    #[cfg(not(windows))]
    {
        let _ = pid;
        Err("stop is only supported on Windows".to_string())
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
    app: AppHandle,
    image: String,
    api_key: String,
    model: String,
    prompt: String,
) -> Result<String, String> {
    // Edited image goes to the app-data images dir, named <source>_<ts>.png.
    let (images_dir, _) = output_dirs(&app)?;
    let out_png = images_dir.join(timestamped_name(&image, "png"));
    tauri::async_runtime::spawn_blocking(move || -> Result<String, String> {
        let mut cmd = engine_command(&app, "ai", "image_process.py")?;
        cmd.arg("--image").arg(&image)
            .arg("--api-key").arg(&api_key)
            .arg("--model").arg(&model)
            .arg("--prompt").arg(&prompt)
            .arg("--out").arg(&out_png)
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
async fn import_json(app: AppHandle, json_path: String) -> Result<String, String> {
    tauri::async_runtime::spawn_blocking(move || -> Result<String, String> {
        let mut cmd = engine_command(&app, "render-json", "render_json.py")?;
        cmd.arg("--json").arg(&json_path)
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

/// Persist a cropped image (a `data:` URL or raw base64 PNG produced in the
/// webview by Semi's Cropper) to the app-data images dir as `<original>_<ts>.png`
/// and return its path, so the result can join the candidate list and be fed to
/// the renderer like any other image. `original` is the cropped source's path
/// (its file stem seeds the name); empty falls back to "image".
#[tauri::command]
async fn save_cropped_image(app: AppHandle, data_url: String, original: String) -> Result<String, String> {
    let (images_dir, _) = output_dirs(&app)?;
    let out_path = images_dir.join(timestamped_name(&original, "png"));
    tauri::async_runtime::spawn_blocking(move || -> Result<String, String> {
        // Accept "data:image/png;base64,XXXX" or a bare base64 payload.
        let b64 = data_url.split_once(',').map(|(_, rest)| rest).unwrap_or(&data_url);
        let bytes = base64::engine::general_purpose::STANDARD
            .decode(b64.trim())
            .map_err(|e| format!("base64 decode failed: {e}"))?;
        std::fs::write(&out_path, &bytes).map_err(|e| format!("write failed: {e}"))?;
        Ok(out_path.to_string_lossy().into_owned())
    })
    .await
    .map_err(|e| format!("save task failed: {e}"))?
}

/// Open the directory containing `path` in the OS file manager (Windows Explorer).
/// Used by the "打开保存目录" button after a render writes its shape JSON.
#[tauri::command]
fn reveal_in_dir(path: String) -> Result<(), String> {
    let p = Path::new(&path);
    let dir = if p.is_dir() {
        p.to_path_buf()
    } else {
        p.parent().map(|d| d.to_path_buf()).unwrap_or_else(|| p.to_path_buf())
    };
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        const CREATE_NO_WINDOW: u32 = 0x0800_0000;
        // Spawn (don't wait): explorer.exe returns exit code 1 even on success,
        // so checking its status would spuriously report failure.
        Command::new("explorer")
            .arg(&dir)
            .creation_flags(CREATE_NO_WINDOW)
            .spawn()
            .map_err(|e| format!("failed to open explorer: {e}"))?;
        Ok(())
    }
    #[cfg(not(windows))]
    {
        let _ = dir;
        Err("reveal is only supported on Windows".to_string())
    }
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_opener::init())
        .plugin(tauri_plugin_dialog::init())
        .manage(EngineState::default())
        .invoke_handler(tauri::generate_handler![start_generation, stop_generation, ai_process_image, read_image_data_url, import_json, save_cropped_image, reveal_in_dir])
        .build(tauri::generate_context!())
        .expect("error while building tauri application")
        .run(|app_handle, event| match event {
            // App is shutting down: kill any still-running sidecar so its GPU
            // (OpenCL context + VRAM) and worker tree are released instead of
            // leaking as an orphan that keeps the GPU busy. Best-effort — there is
            // no UI left to surface an error to. `ExitRequested` fires when the
            // last window closes; `Exit` is the final stop for every exit path.
            tauri::RunEvent::ExitRequested { .. } | tauri::RunEvent::Exit => {
                let pid = *app_handle.state::<EngineState>().pid.lock().unwrap();
                if let Some(pid) = pid {
                    let _ = kill_pid(pid);
                }
            }
            _ => {}
        });
}
