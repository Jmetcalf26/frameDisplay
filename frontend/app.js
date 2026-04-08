const cover = document.getElementById("cover");
const title = document.getElementById("title");
const artist = document.getElementById("artist");
const meta = document.getElementById("meta");
const display = document.getElementById("display");

let currentKey = null;

function connect() {
    const ws = new WebSocket(`ws://${location.host}/ws`);

    ws.onmessage = (event) => {
        const data = JSON.parse(event.data);
        update(data);
    };

    ws.onclose = () => setTimeout(connect, 3000);
    ws.onerror = () => ws.close();
}

function update(data) {
    if (data.state === "idle") {
        display.className = "idle";
        currentKey = null;
        return;
    }

    if (!data.track) return;

    const key = `${data.track.artist}:${data.track.title}`.toLowerCase();
    if (key === currentKey) return;
    currentKey = key;

    const img = new Image();
    img.onload = () => {
        cover.src = img.src;
        title.textContent = data.track.title || "";
        artist.textContent = data.track.artist || "";

        const parts = [data.track.label, data.track.year, data.track.genre].filter(Boolean);
        meta.textContent = parts.join(" \u00b7 ");

        display.className = "identified";
    };
    img.src = data.track.cover_url || "";
}

connect();
