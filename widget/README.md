# Rafiq's widget

A small window that shows what Rafiq is doing as it happens. The small view shows Rafiq's state and the step
running now. The large view shows the whole request: what you asked, the plan and its progress, each step,
an explanation, cards and the answer. Arabic is shown right to left.

In the large view you can type a request, and answer Rafiq's questions with Yes, No or Always (Always only
when Rafiq may remember it for that tool). The stop button, shown while Rafiq is busy, cuts its speech and
keeps it from running the rest of the request: a step already running finishes, and a question waiting is
answered no. A request typed that can't be sent stays in the box. A private tool's result
shows as "(hidden)".

## How it works

- The widget is a separate app. Rafiq's core stays in Python.
- The Rust side (`src-tauri/`) reads the panel token from `~/.config/rafiq/panel.token` (`RAFIQ_HOME` moves
  it) and the port from `[panel] port` in `config.toml`, the files `rafiq ask` uses. It follows the event stream
  at `/events` over the panel's socket, `~/.config/rafiq/run/panel.sock` (see [docs/events.md](../docs/events.md)),
  and connects again when Rafiq restarts.
- The page (`ui/`) is plain HTML and JavaScript with no packages. It only gets the events, never the token, and
  draws everything as text.
- The page asks the Rust side for one of three things: a typed request, an answer, or stop. It never chooses
  a path, and the Rust side adds the token.
- It only connects on this computer. A panel set to listen anywhere else is refused.
- Before it sends the token, it checks that the socket's folder is yours, isn't a link and is closed to others,
  and that the socket is yours: then no other account can have made it (#234). The token is kept between
  Rafiq's starts, so while Rafiq is stopped another account's program could otherwise wait where Rafiq listens
  to collect it. While Rafiq is away it tries again after 2 seconds, then less often, up to every 30.
- On Windows (#242) the settings are in `%APPDATA%\rafiq`, and it reaches Rafiq through its named pipe,
  `\\.\pipe\rafiq-panel-<your SID>`, never the panel's port. Before it sends the token, it reads the pipe's own
  security descriptor: it must be owned by your account, open to it alone, and labelled Medium or higher, as Rafiq
  makes it. Another account can't make a pipe owned by yours, and a sandboxed program's is labelled Low.
- When Rafiq looks at the screen, the Rust side fetches the screenshot once with the panel's token and the
  screen token (`screen.token`, readable only by you), and hands the page a `data:` URL. The page draws
  numbered circles, highlights and arrows over it.
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
