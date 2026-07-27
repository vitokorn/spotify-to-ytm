import concurrent.futures
import threading
from dataclasses import dataclass
from urllib.parse import quote, urlencode

from fasthtml.common import *
from fasthtml.pico import picolink

from thefuzz import fuzz

from src.spotify import SpotifyManager
from src.yt_music import YT_Music, YTMusicAuthError

custom_styles = Style("""
    :root {
        --primary: #1db954;
        --primary-hover: #1ed760;
        --bg-card: rgba(255, 255, 255, 0.04);
        --border-card: rgba(255, 255, 255, 0.1);
    }
    .status-mismatch {
        background: rgba(239, 68, 68, 0.25) !important;
        color: #f87171 !important;
        border: 1px solid rgba(239, 68, 68, 0.4) !important;
    }
    .text-mismatch {
        color: #f87171 !important;
        font-weight: 500;
    }
    .app-header {
        display: flex;
        justify-content: space-between;
        align-items: center;
        margin-bottom: 1.5rem;
        padding-bottom: 1rem;
        border-bottom: 1px solid var(--border-card);
    }
    .filter-bar {
        display: flex;
        gap: 0.5rem;
        flex-wrap: wrap;
        align-items: center;
        margin-bottom: 1.5rem;
    }
    .filter-btn {
        padding: 0.4rem 1rem;
        font-size: 0.85rem;
        border-radius: 20px;
        border: 1px solid var(--border-card);
        background: transparent;
        cursor: pointer;
        color: inherit;
        transition: all 0.2s ease;
    }
    .filter-btn.active, .filter-btn:hover {
        background: var(--primary);
        color: #fff;
        border-color: var(--primary);
    }
    .search-box {
        width: 100%;
        max-width: 400px;
        padding: 0.5rem 1rem;
        border-radius: 20px;
        border: 1px solid var(--border-card);
        font-size: 0.95rem;
        margin-bottom: 1rem;
    }
    .card-grid {
        display: grid;
        grid-template-columns: repeat(auto-fill, minmax(260px, 1fr));
        gap: 1.2rem;
        margin-bottom: 2rem;
    }
    .item-card {
        border: 1px solid var(--border-card);
        border-radius: 10px;
        padding: 1.2rem;
        background: var(--bg-card);
        display: flex;
        flex-direction: column;
        justify-content: space-between;
        transition: transform 0.2s, border-color 0.2s;
    }
    .item-card:hover {
        transform: translateY(-3px);
        border-color: var(--primary);
    }
    .card-title {
        font-weight: 600;
        font-size: 1.05rem;
        margin-bottom: 0.5rem;
        word-break: break-word;
    }
    .card-footer {
        display: flex;
        justify-content: space-between;
        align-items: center;
        margin-top: 1rem;
    }
    .badge {
        font-size: 0.72rem;
        padding: 0.2rem 0.6rem;
        border-radius: 12px;
        font-weight: 600;
        text-transform: uppercase;
        letter-spacing: 0.5px;
    }
    .badge-own { background: rgba(59, 130, 246, 0.2); color: #93c5fd; border: 1px solid rgba(59, 130, 246, 0.4); }
    .badge-spotify { background: rgba(16, 185, 129, 0.2); color: #6ee7b7; border: 1px solid rgba(16, 185, 129, 0.4); }
    .status-badge { font-size: 0.8rem; padding: 0.2rem 0.5rem; border-radius: 4px; font-weight: 600; }
    .status-matched { background: rgba(34, 197, 94, 0.2); color: #4ade80; }
    .status-missing { background: rgba(234, 179, 8, 0.2); color: #fde047; }
    .refresh-btn-link {
        display: inline-flex;
        align-items: center;
        gap: 0.4rem;
        font-size: 0.9rem;
    }
""")

icon_link = Link(
    rel="stylesheet", href="https://www.nerdfonts.com/assets/css/webfont.css"
)
app, rt = fast_app(debug=True, hdrs=(picolink, icon_link, custom_styles))

user_confirm_login_spot = is_initialized = False
login_statuses = {"status": None, "type": None}
spot = yt = None
loaded_library = False
current_playlist_title = None


def session_ready():
    return is_initialized and spot is not None and yt is not None


def loading_library_view():
    return Div(
        P(
            Strong("Thanks for logging In!"),
            Br(),
            "Please wait while we load your Spotify library...",
            Br(),
            "This usually takes 5-10 seconds.",
            hx_trigger="every 1s",
            hx_get="/is_library_built",
            hx_swap="innerHTML",
            hx_target=".main-view",
        ),
        cls="main-view",
    )


def library_section(title, items):
    if not items:
        return None

    card_elements = []
    for item in items:
        if isinstance(item, dict):
            name = item.get("name", "")
            uri = item.get("uri", "")
            owner_name = item.get("owner", "")
            if "is_own" in item:
                is_own = bool(item["is_own"])
            elif owner_name:
                is_own = owner_name.lower() not in {"spotify", "spotify soundtrack", "editorial"}
            else:
                is_own = True
                owner_name = "You"
        else:
            name = str(item)
            uri = str(item)
            is_own = True
            owner_name = "You"

        owner_type = "own" if is_own else "spotify"
        badge_cls = "badge badge-own" if is_own else "badge badge-spotify"
        badge_label = "My Playlist" if is_own else f"By {owner_name or 'Spotify'}"

        card = Div(
            Div(
                Div(name, cls="card-title"),
                Span(badge_label, cls=badge_cls) if title == "Playlists" else None,
            ),
            Div(
                A(
                    "Convert",
                    hx_get=f"/uri/{uri}?title={quote(name)}",
                    hx_target=".main-view",
                    cls="button outline primary",
                    style="margin: 0; padding: 0.3rem 0.8rem; font-size: 0.85rem;",
                ),
                cls="card-footer",
            ),
            cls="item-card",
            data_owner=owner_type,
            data_title=name.lower(),
        )
        card_elements.append(card)

    return Div(
        H3(f"{title} ({len(items)})", id="playlists-section-title" if title == "Playlists" else None),
        Div(*card_elements, cls="card-grid"),
        data_section="playlists" if title == "Playlists" else title.lower(),
        style="margin-bottom: 2rem;",
    )


def spotify_error_view(title, error):
    if isinstance(error, str) and error.startswith("rate limited"):
        message = f"Spotify is rate limiting this request; {error}."
    elif str(error) == "401" or "401" in str(error):
        message = "Spotify authorization token has expired (401). Click below to re-authenticate session instantly without restarting."
    else:
        message = f"Spotify returned error {error}. You can re-authenticate your session below."

    return Div(
        Titled(title),
        P(message),
        Div(
            A("Re-authenticate Session", hx_get="/reauth", hx_target=".main-view", cls="button primary", style="margin-right: 0.5rem;"),
            A("Go Back to Library", hx_get="/library", hx_target=".main-view", cls="button outline"),
        ),
        cls="main-view",
    )


@app.get("/reauth")
def reauth():
    global spot, is_initialized, loaded_library
    if os.path.exists("spotify_auth.json"):
        try:
            os.remove("spotify_auth.json")
        except OSError:
            pass
    is_initialized = False
    loaded_library = False
    spot = None
    threading.Thread(target=start_sess, daemon=True).start()
    return RedirectResponse("/check_auth")


def liked_songs_section():
    if not spot or not spot.library.get("HasLikedSongs"):
        return None

    return Div(
        H3("Liked Songs"),
        Div(
            Div(
                Div("Your Liked Songs Collection", cls="card-title"),
                Span("Liked Songs", cls="badge badge-own"),
            ),
            Div(
                A(
                    "Convert Liked Songs",
                    hx_get="/uri/liked?title=Liked+Songs",
                    hx_target=".main-view",
                    cls="button outline primary",
                    style="margin: 0; padding: 0.3rem 0.8rem; font-size: 0.85rem;",
                ),
                cls="card-footer",
            ),
            cls="item-card",
            data_owner="own",
            data_title="liked songs",
            style="max-width: 320px; margin-bottom: 2rem;",
        ),
    )


def start_sess():
    global is_initialized, login_statuses, spot, yt
    is_initialized = False
    spot = SpotifyManager()
    yt = YT_Music()
    is_initialized = True
    login_statuses["status"] = True
    login_statuses["type"] = None


@app.get("/")
def get():
    global is_initialized, spot, yt
    if not session_ready() and os.path.exists("spotify_auth.json"):
        try:
            spot = SpotifyManager()
            yt = YT_Music()
            is_initialized = True
        except Exception as e:
            print(f"Auto-session init failed: {e}")

    if session_ready():
        return RedirectResponse("/library")

    return Titled(
        "Spotify to YTM",
        Div(
            P("Welcome! Click 'Start Application' to log in."),
            Button(
                "Start Application",
                hx_get="/start",
                hx_swap="innerHTML",
                hx_target=".main-view",
                id="start-button",
                cls="primary",
            ),
            cls="main-view",
        ),
    )


@app.get("/check_auth")
def get():
    global login_statuses
    if login_statuses["status"] is None:
        return P(
            "A new browser window will open now.",
            Br(),
            "Please wait while we check your login status...",
            hx_get="/check_auth",
            hx_swap="outerHTML",
            hx_trigger="every 1s",
        )

    elif login_statuses["type"] == "ytm":
        return (
            H2("Login Instructions"),
            Div(
                P(
                    "It seems you are logged in to Spotify but not to YouTube Music. Please follow the instructions below to login."
                ),
                Ul(
                    Li("Switch to the browser window that opened."),
                    Li("Log in to your Google Account on YouTube Music."),
                    Li("Click 'Done' below after logging in."),
                ),
            ),
            Button(
                "Done",
                hx_get="/user_confirm_login",
                id="done-button",
                hx_swap="innerHTML",
                hx_target=".main-view",
            ),
        )

    elif not login_statuses["status"]:
        return (
            H2("Login Instructions"),
            Div(
                P("Please follow the instructions below to login."),
                Ul(
                    Li("Switch to the Chrome browser window opened by the app."),
                    Li("Login with your Spotify account."),
                    Li("In another tab, login to your YouTube Music Google account."),
                    Li("Switch back here and click 'Done'."),
                ),
            ),
            Button(
                "Done",
                hx_get="/user_confirm_login",
                id="done-button",
                hx_swap="innerHTML",
                hx_target=".main-view",
            ),
        )

    else:
        return RedirectResponse("/user_confirm_login")


@app.get("/update_login")
def get(status: bool = None, type: str = None):
    global login_statuses
    login_statuses["status"] = status
    login_statuses["type"] = type


@app.get("/start")
def get():
    global is_initialized, loaded_library, login_statuses, spot, yt, user_confirm_login_spot
    if session_ready():
        return RedirectResponse("/library")
    is_initialized = False
    loaded_library = False
    login_statuses = {"status": None, "type": None}
    spot = yt = None
    user_confirm_login_spot = False
    threading.Thread(target=start_sess, daemon=True).start()
    return RedirectResponse("/check_auth")


@app.get("/check_user_confirmation")
def get():
    global user_confirm_login_spot
    return {"confirmed": user_confirm_login_spot}


@app.get("/user_confirm_login")
def get():
    global user_confirm_login_spot
    user_confirm_login_spot = True
    return loading_library_view()


@app.get("/is_library_built")
def get():
    if not session_ready():
        return loading_library_view()
    else:
        return RedirectResponse("/library")


@app.get("/refresh_library")
def refresh_library():
    global spot, loaded_library
    if not session_ready():
        return loading_library_view()

    spot.refresh_library()
    loaded_library = True
    return RedirectResponse("/library")


@app.get("/library")
def get():
    global spot, loaded_library
    if not session_ready():
        return loading_library_view()

    library = spot.library

    search_and_filter_js = Script("""
        function filterByOwner(ownerType, btnElement) {
            document.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
            if (btnElement) btnElement.classList.add('active');
            window.currentFilterOwner = ownerType;
            applyFilters();
        }

        function applyFilters() {
            const query = (document.getElementById('playlist-search')?.value || '').toLowerCase().trim();
            const filterOwner = window.currentFilterOwner || 'all';
            let visiblePlaylists = 0;

            document.querySelectorAll('.item-card').forEach(card => {
                const cardOwner = card.dataset.owner;
                const cardTitle = card.dataset.title || '';
                const ownerMatch = (filterOwner === 'all') ||
                                   (filterOwner === 'own' && cardOwner === 'own') ||
                                   (filterOwner === 'spotify' && cardOwner === 'spotify');
                const searchMatch = !query || cardTitle.includes(query);
                const isVisible = ownerMatch && searchMatch;
                card.style.display = isVisible ? 'flex' : 'none';
                if (isVisible && card.closest('[data-section="playlists"]')) {
                    visiblePlaylists++;
                }
            });

            const titleEl = document.getElementById('playlists-section-title');
            if (titleEl) {
                let label = 'Playlists';
                if (filterOwner === 'own') label = 'My Playlists';
                else if (filterOwner === 'spotify') label = 'Spotify / Saved Playlists';
                titleEl.textContent = `${label} (${visiblePlaylists})`;
            }
        }
    """)

    header = Div(
        Div(
            H2("Spotify Library", style="margin: 0;"),
            P("Select a playlist, album, or liked songs to convert to YouTube Music.", style="margin: 0; color: #a0a0a0;"),
        ),
        A(
            I(cls="nf nf-md-reload"),
            " Refresh Library",
            hx_get="/refresh_library",
            hx_target=".main-view",
            hx_swap="innerHTML",
            cls="button outline refresh-btn-link",
            style="margin: 0;",
        ),
        cls="app-header",
    )

    filter_controls = Div(
        Input(
            type="text",
            id="playlist-search",
            placeholder="Search playlists, albums, artists...",
            oninput="applyFilters()",
            cls="search-box",
        ),
        Div(
            Button("All Items", onclick="filterByOwner('all', this)", cls="filter-btn active"),
            Button("My Playlists", onclick="filterByOwner('own', this)", cls="filter-btn"),
            Button("Spotify / Saved", onclick="filterByOwner('spotify', this)", cls="filter-btn"),
            cls="filter-bar",
        ),
    )

    layout = Div(
        header,
        filter_controls,
        liked_songs_section(),
        library_section("Playlists", library.get("Playlists", [])),
        library_section("Albums", library.get("Albums", [])),
        library_section("Artists", library.get("Artists", [])),
        search_and_filter_js,
        cls="main-view",
    )
    loaded_library = True
    return Title("Spotify Library"), layout


@app.get("/initialized")
def get():
    global is_initialized
    is_initialized = True
    return {"status": "success"}


@app.get("/uri/{uri}")
def get(uri: str, title: str = "Liked Songs"):
    global current_playlist_title
    if not session_ready():
        return loading_library_view()

    current_playlist_title = title
    if "playlist" in uri:
        return LibraryItem(title, uri, "playlist")
    if "album" in uri:
        return LibraryItem(title, uri, "album")
    if "artist" in uri:
        return LibraryItem(title, uri, "artist")
    else:
        return LibraryItem(title, uri, "liked")


def calculate_match_quality(orig_title, orig_artist, new_title, new_artist, confidence=100):
    if not new_title or not str(new_title).strip():
        return 0, True

    orig_str = f"{orig_title} {orig_artist}".lower()
    new_str = f"{new_title} {new_artist}".lower()

    set_ratio = fuzz.token_set_ratio(orig_str, new_str)
    title_ratio = fuzz.token_set_ratio(str(orig_title).lower(), str(new_title).lower())

    score = int((set_ratio * 0.6) + (title_ratio * 0.4))
    is_mismatch = (score < 50 or confidence < 50)
    return score, is_mismatch


def fetch_song(item):
    song_title, artist_name = item
    try:
        title, artist, confidence, vid_id = yt.search_one(f"{song_title} ,{artist_name}")
        score, is_mismatch = calculate_match_quality(song_title, artist_name, title, artist, confidence)
        is_selected = not is_mismatch
        print(f"Fetched Song: '{song_title}' -> '{title}' | score={score}% mismatch={is_mismatch}")
        return [title, artist, is_selected, vid_id, score, is_mismatch]
    except Exception as e:
        print(f"Failed to fetch '{song_title}' - '{artist_name}': {e}")
        return ["", "", False, None, 0, True]


def fetch_equivalents(uri: str):
    global spot, yt, new_playlist
    if not session_ready():
        return

    if "playlist" in uri:
        items, _ = spot.get_playlist(uri)
    elif "album" in uri:
        items, _ = spot.get_albums(uri)
    elif "artist" in uri:
        items, _ = spot.get_artists(uri)
    else:
        items, _ = spot.get_liked()
    if not isinstance(items, list):
        print(f"Spotify returned error {items} while fetching {uri}.")
        return

    new_playlist = {"title": "", "desc": "", "items": []}
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        results = executor.map(fetch_song, items)
        new_playlist["items"].extend(results)


@dataclass
class LibraryItem:
    title: str
    uri: str
    uri_type: str

    def __ft__(self):
        global spot, old_playlist
        if not session_ready():
            return loading_library_view()

        if self.uri_type == "playlist":
            items, _ = spot.get_playlist(self.uri)
        elif self.uri_type == "album":
            items, _ = spot.get_albums(self.uri)
        elif self.uri_type == "artist":
            items, _ = spot.get_artists(self.uri)
        else:
            items, _ = spot.get_liked()
        if not isinstance(items, list):
            return spotify_error_view(self.title, items)

        old_playlist = items
        layout = Div(
            Div(
                A("← Back to Library", hx_get="/library", hx_target=".main-view", cls="button outline", style="margin-bottom: 1rem;"),
                H2(f"{self.title} ({len(items)} tracks)"),
                P("Review tracks below and click 'Fetch All YouTube Equivalents' to start matching."),
            ),
            Table(
                Tr(
                    Th("Original Title"),
                    Th("Original Artists"),
                    Th("Matched Title"),
                    Th("Matched Artists"),
                ),
                *[
                    Tr(
                        Td(item[0]),
                        Td(item[1]),
                        Td("-", cls="yt-title", style="color: #888;"),
                        Td("-", cls="yt-artists", style="color: #888;"),
                    )
                    for item in items
                ],
                id="#item-table",
            ),
            Div(
                Button(
                    "Fetch All YouTube Equivalents",
                    hx_get=f"/start_fetch_equi?uri={self.uri}",
                    hx_swap="outerHTML",
                    cls="primary",
                ),
                style="margin-top: 1.5rem;",
            ),
            Div(cls="yt-info-box"),
            cls="main-view",
        )
        return layout


@app.get("/new_table")
def get():
    global new_playlist, old_playlist
    fetched_count = len(new_playlist["items"])
    total_count = len(old_playlist)

    table_rows = []
    for idx, item in enumerate(old_playlist):
        is_fetched = (fetched_count >= idx + 1)
        if is_fetched:
            song_data = new_playlist["items"][idx]
            new_title = song_data[0]
            new_artist = song_data[1]
            is_checked = song_data[2]
            score = song_data[4] if len(song_data) >= 5 else 100
            is_mismatch = song_data[5] if len(song_data) >= 6 else (score < 50)
        else:
            new_title = "Matching..."
            new_artist = "..."
            is_checked = False
            score = 100
            is_mismatch = False

        row_style = "background: rgba(239, 68, 68, 0.14); border-left: 4px solid #ef4444;" if (is_fetched and is_mismatch) else None
        title_cls = "yt-title text-mismatch" if (is_fetched and is_mismatch) else "yt-title"
        artist_cls = "yt-artists text-mismatch" if (is_fetched and is_mismatch) else "yt-artists"

        if is_fetched:
            if is_mismatch:
                status_el = Span(f"Mismatch ({score}%)", cls="status-badge status-mismatch")
            elif is_checked:
                status_el = Span("Matched", cls="status-badge status-matched")
            else:
                status_el = Span("Unselected", cls="status-badge status-missing")
        else:
            status_el = Span("Pending", cls="status-badge status-missing")

        row = Tr(
            Td(
                Input(
                    type="checkbox",
                    data_idx=NotStr(str(idx)),
                    checked=is_checked if is_fetched else False,
                )
            ),
            Td(item[0]),
            Td(item[1]),
            Td(new_title, cls=title_cls),
            Td(new_artist, cls=artist_cls),
            Td(status_el),
            Td(
                Button(
                    I(cls="nf nf-md-reload"),
                    title="Refetch song match",
                    hx_get=f"/refetch_item?title={quote(item[0])}&artist={quote(item[1])}&filter_str={quote(str(new_title) + ', ' + str(new_artist))}&idx={idx}",
                    hx_swap="outerHTML",
                    hx_target="table",
                    cls="outline",
                    style="padding: 0.2rem 0.5rem; margin: 0;",
                )
                if is_fetched
                else None,
            ),
            style=row_style,
        )
        table_rows.append(row)

    table = Table(
        Tr(
            Th("Select"),
            Th("Original Title"),
            Th("Original Artists"),
            Th("New Title"),
            Th("New Artists"),
            Th("Status"),
            Th("Refetch"),
        ),
        *table_rows,
        id="#item-table",
    )

    if fetched_count == total_count:
        selected_ids = [
            idx for idx, item in enumerate(new_playlist["items"]) if item[2] is True
        ]
        params = {"selectedIds": selected_ids}
        hx_get_url = f"/save_selection?{urlencode(params, doseq=True)}"
        script = """document.addEventListener('change', function(e) {
    if (e.target.type === 'checkbox') {
        const updateBtn = document.getElementById('update-btn');
        const checkedBoxes = document.querySelectorAll('input[type="checkbox"]:checked');
        const selectedIndices = Array.from(checkedBoxes).map(box => box.dataset.idx);
        if (selectedIndices.length > 0) {
            const params = new URLSearchParams();
            selectedIndices.forEach(idx => {
                params.append('selectedIds', idx);
            });
            const newUrl = `/save_selection?${params.toString()}`;
            const bgUrl = `/bg_save?${params.toString()}`;
            updateBtn.setAttribute('hx-get', newUrl);
            htmx.process(updateBtn);
            updateBtn.disabled = false;
            htmx.ajax('GET', bgUrl, { swap: 'none', target: 'body' });
        } else {
            updateBtn.disabled = true;
            updateBtn.removeAttribute('hx-get');
            htmx.process(updateBtn);
        }
    }
});"""
        return (
            table,
            Button(
                "Save Selection & Create Playlist",
                id="update-btn",
                hx_swap_oob="true",
                hx_get=hx_get_url,
                hx_target=".yt-info-box",
                cls="primary",
            ),
            Script(script),
        )
    else:
        return (
            table,
            Button(
                f"Fetching ({fetched_count}/{total_count})...",
                id="update-btn",
                hx_swap_oob="true",
                disabled=True,
                hx_get="/new_table",
                hx_trigger="every 0.5s",
                hx_swap="outerHTML",
                hx_target="table",
                cls="secondary",
            ),
        )


@app.get("/start_fetch_equi")
def get(uri: str):
    threading.Thread(target=fetch_equivalents, daemon=True, kwargs={"uri": uri}).start()
    return Button(
        "Starting Matcher...",
        hx_get="/new_table",
        hx_trigger="every 1s",
        hx_swap="outerHTML",
        hx_target="table",
        id="update-btn",
        disabled=True,
        cls="secondary",
    )


@app.get("/refetch_item")
def get(title: str, artist: str, filter_str: str, idx: int):
    global yt, new_playlist
    try:
        new_title, new_artist, confidence, video_id = yt.search_one_except(
            f"{title} {artist}", filter_str
        )
        score, is_mismatch = calculate_match_quality(title, artist, new_title, new_artist, confidence)
        is_selected = not is_mismatch
        item_data = [new_title, new_artist, is_selected, video_id, score, is_mismatch]
        if idx < len(new_playlist["items"]):
            new_playlist["items"][idx] = item_data
    except Exception as e:
        print(f"Refetch failed: {e}")
    return RedirectResponse("/new_table")


@app.get("/bg_save")
def save_selection(req):
    global new_playlist
    selected_ids = req.query_params.multi_items()
    selected_ids = [int(value) for key, value in selected_ids if key == "selectedIds"]
    for idx, _ in enumerate(new_playlist["items"]):
        if idx not in selected_ids:
            new_playlist["items"][idx][2] = False
        else:
            new_playlist["items"][idx][2] = True


@app.get("/save_selection")
def save_selection(req):
    global new_playlist
    selected_ids = req.query_params.multi_items()
    selected_ids = [int(value) for key, value in selected_ids if key == "selectedIds"]
    for idx, _ in enumerate(new_playlist["items"]):
        if idx not in selected_ids:
            new_playlist["items"][idx][2] = False
        else:
            new_playlist["items"][idx][2] = True
    form = Form(
        Label(
            "Playlist Title on YouTube Music",
            Input(type="text", name="title", value=current_playlist_title),
        ),
        Label("Description (Optional)", Input(type="text", name="desc")),
        Button("Create YouTube Music Playlist", cls="primary"),
        action="/make_playlist",
        method="post",
        style="margin-top: 1rem;",
    )

    return Div(
        P("Selection saved! Confirm the title and description below to create your playlist on YouTube Music:"),
        form,
    )


@app.post("/make_playlist")
def make_playlist(title: str, desc: str = ""):
    global new_playlist, yt
    items = new_playlist.get("items", [])
    vid_ids = [item[3] for item in items if isinstance(item, list) and len(item) >= 4 and item[2] is True and item[3]]
    if not vid_ids:
        return P("No valid tracks selected to create playlist on YouTube Music.")

    try:
        created = yt.create_and_add(title, desc, vid_ids)
    except YTMusicAuthError:
        return (
            H2("YouTube Music Login Needed"),
            P("YouTube Music rejected the playlist creation request."),
            P("In the Chrome window opened by this app, sign in at music.youtube.com, then restart from the Start button."),
            Button("Restart Login Flow", hx_get="/start", hx_target=".main-view"),
        )
    except Exception as e:
        print(f"Playlist creation exception: {e}")
        return P(f"Error creating playlist: {e}")

    if created:
        return (
            H2("Successfully Created Your Playlist!"),
            P("You can now view your new playlist on YouTube Music."),
            P("Redirecting to library in 5 seconds..."),
            Script("setTimeout(() => { window.location.href = '/library'; }, 5000)"),
        )
    else:
        return P("An error occurred while creating the YouTube Music playlist.")


if __name__ == "__main__":
    import uvicorn
    print("Starting Spotify to YTM server on http://localhost:5001 ...")
    uvicorn.run(app, host="0.0.0.0", port=5001)
