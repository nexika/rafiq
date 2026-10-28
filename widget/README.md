# Rafiq's widget

A small window that shows what Rafiq is doing as it happens. The small view shows Rafiq's state and the step
running now. The large view shows the whole request: what you asked, the plan and its progress, each step,
an explanation, cards and the answer. Arabic is shown right to left.

This is the first version (#199). It shows; it doesn't ask yet. Typed requests, the Yes, No and Always buttons,
and a stop button come next.

## How it works

- The widget is a separate app. Rafiq's core stays in Python.
- The Rust side (`src-tauri/`) reads the panel token from `~/.config/rafiq/panel.token` (`RAFIQ_HOME` moves
  it) and the port from `[panel] port` in `config.toml`, the files `rafiq ask` uses. It follows the event stream
  at `http://127.0.0.1:<port>/events` (see [docs/events.md](../docs/events.md)) and connects again when Rafiq
  restarts.
- The page (`ui/`) is plain HTML and JavaScript with no packages. It only gets the events, never the token, and
  draws everything as text.
- It only connects on this computer. A panel set to listen anywhere else is refused.
- Before it sends the token, it checks in `/proc/net/tcp` that the program listening on the panel's port is
  yours. The token is kept between Rafiq's starts, so while Rafiq is stopped another program could take the
  port to collect it. While Rafiq is away it tries again after 2 seconds, then less often, up to every 30.
- Its own words (the state, problems, labels) are in Rafiq's six languages, following the request's language.
- On GNOME Wayland it is an ordinary window, since an app can't keep itself above the others there.

## Build and run

You need Rust, Node 22 for the tests, and WebKitGTK 4.1 (`webkit2gtk4.1-devel` on Fedora,
`libwebkit2gtk-4.1-dev` on Debian and Ubuntu). Start Rafiq with `rafiq run`, then:

```bash
cd widget/src-tauri
cargo run --release
```

## Tests

```bash
cd widget && node --test          # what the page shows for each event (ui/model.js)
cd widget/src-tauri && cargo test # finding the panel, and reading the stream
```

## Measured (Fedora 44, GNOME Wayland)

- The release binary is 6.0 MB. It uses the system's WebKitGTK.
- It connected to the panel 0.6 s after launch.
