import json
import os
import re
import requests
import shutil
import subprocess
import urllib
import time
import certifi

os.environ.setdefault("SSL_CERT_FILE", certifi.where())

import undetected_chromedriver as uc
from os import path
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, NoSuchElementException, WebDriverException
from selenium.webdriver.common.desired_capabilities import DesiredCapabilities


def _detect_chrome_major_version():
    browser_paths = [
        os.environ.get("CHROME_BINARY"),
        shutil.which("google-chrome"),
        shutil.which("google-chrome-stable"),
        shutil.which("chromium"),
        shutil.which("chromium-browser"),
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Google Chrome Canary.app/Contents/MacOS/Google Chrome Canary",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
    ]

    for browser_path in filter(None, browser_paths):
        if not path.exists(browser_path):
            continue
        try:
            result = subprocess.run(
                [browser_path, "--version"],
                check=True,
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (OSError, subprocess.SubprocessError):
            continue

        match = re.search(r"(\d+)\.", result.stdout)
        if match:
            return int(match.group(1))

    return None


class SetupManager:

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._webdriver_running = False
        return cls._instance
    
    def __init__(self):
        if not self._webdriver_running:
            d = DesiredCapabilities.CHROME
            d['goog:loggingPrefs'] = { 'performance':'ALL' } # this took way too long
            pwd = path.dirname(__file__)
            version_main = int(os.environ.get("CHROME_VERSION_MAIN") or 0) or _detect_chrome_major_version()
            self.driver = uc.Chrome(
                user_data_dir=f"{pwd}{path.sep}webdriver_profile2",
                desired_capabilities=d,
                version_main=version_main,
            )
            self._webdriver_running = True

            self._login_spotify()
            self.library = {
                'Albums' : [],
                'Artists' : [],
                'Folders' : [],
                'HasLikedSongs' : False,
                'Playlists' : [],
                'TrashItems' : 0
            }

            self.has_p_keys = False
            self._seen_operations = []
            self.operation_names = {}
            self.spotify_request_headers = {}

            self.persisted_qs = {
                'Albums' : '',
                'Artists' : '',
                'LikedSongs' : '',
                'Playlists' : '',
            }
            self.liked_uri = ''
            self.liked_variables = {}
            self.liked_songs = []
            self.yt_cookies = None

        if not self.yt_cookies:
            self._get_ytm_cookies()

    def __exit__(self):
        if self._webdriver_running:
            self.driver.quit()
            self._webdriver_running = False
        return self

    def _safe_get(self, url, attempts=3):
        last_error = None
        for attempt in range(attempts):
            try:
                self.driver.get(url)
                return True
            except WebDriverException as error:
                last_error = error
                if "target frame detached" not in str(error).lower() or attempt == attempts - 1:
                    raise
                time.sleep(1)
        raise last_error

    def _login_spotify(self):
        # opening the homepage
        self._safe_get('https://open.spotify.com')
        
        #checking if alraedy logged in
        logged_in = True
        try:
            # we wait for the login button
            WebDriverWait(self.driver, 5).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, '[data-testid="login-button"]'))
            )
            logged_in = False
        except TimeoutException:
            logged_in = True
        if not logged_in:
            user_confirm = False
            requests.get("http://localhost:5001/update_login?status=false&type=spotify")
            while not user_confirm:
                time.sleep(1)
                res = requests.get("http://localhost:5001/check_user_confirmation")
                user_confirm = "true" in res.text
        return logged_in
    
    @staticmethod
    def _get_header(headers, name):
        for key, value in headers.items():
            if key.lower() == name.lower():
                return value
        return None

    @staticmethod
    def _extract_auth(url,headers):
        try:
            auth = SetupManager._get_header(headers, 'authorization')
            c_token = SetupManager._get_header(headers, 'client-token')
            if not auth or not c_token:
                raise KeyError
            temp_url = urllib.parse.unquote_plus(url)
            url_j = json.loads('{"id": ' + temp_url.split('&variables=')[1].replace('&extensions=', ',"extensions":') + '}')
            persisted =  json.dumps(url_j['extensions'])
            return c_token, auth, persisted
        except KeyError:
            temp_url = urllib.parse.unquote_plus(url)
            url_j = json.loads('{"id": ' + temp_url.split('&variables=')[1].replace('&extensions=', ',"extensions":') + '}')
            persisted =  json.dumps(url_j['extensions'])
            return None, None, json.dumps(url_j['extensions'])

    @staticmethod
    def _extract_auth_from_body(body, headers):
        try:
            body_json = json.loads(body)
            auth = SetupManager._get_header(headers, 'authorization')
            c_token = SetupManager._get_header(headers, 'client-token')
            if not auth or not c_token:
                raise KeyError
            persisted_query = json.dumps(body_json['extensions'])
            return c_token, auth, persisted_query
        except KeyError:
            raise KeyError("Could not extract authorization or client token from the request body.")
        
    @staticmethod
    def _extract_operation_name(url, body):
        if "operationName=" in url:
            return urllib.parse.parse_qs(urllib.parse.urlparse(url).query).get("operationName", [None])[0]
        if body:
            try:
                return json.loads(body).get("operationName")
            except json.JSONDecodeError:
                return None
        return None

    @staticmethod
    def _operation_matches(actual_name, expected_name):
        if actual_name == expected_name:
            return True
        if expected_name == "fetchLibraryTracks" and actual_name:
            normalized = actual_name.lower()
            return "library" in normalized and ("track" in normalized or "song" in normalized)
        if expected_name == "libraryV3" and actual_name:
            return "library" in actual_name.lower()
        return False

    def _get_logged_operations(self):
        operations = list(getattr(self, "_seen_operations", []))
        logs = self.driver.get_log("performance")
        for log in logs:
            message = json.loads(log["message"])["message"]
            if message["method"] != "Network.requestWillBeSent":
                continue
            request = message["params"]["request"]
            operation_name = self._extract_operation_name(
                request["url"],
                request.get("postData", ""),
            )
            if operation_name:
                operations.append(operation_name)
        self._seen_operations = operations
        return operations

    def _print_spotify_debug_info(self):
        operations = self._get_logged_operations()
        unique_operations = sorted(set(operations))
        print(f"Current Spotify URL: {self.driver.current_url}")
        print(f"Captured {len(operations)} Spotify operation request(s).")
        if unique_operations:
            print("Captured operation names:")
            for operation in unique_operations[:50]:
                print(f"- {operation}")
        else:
            print("No Spotify operation names were captured from the browser logs.")

    def _extract_auth_from_network_logs(self, operation_name, raise_on_missing=True):
        logs = self.driver.get_log("performance")
        for log in logs:
            message = json.loads(log["message"])["message"]
            if message["method"] == "Network.requestWillBeSent":
                request = message["params"]["request"]
                url = request["url"]
                body = request.get("postData", "")
                actual_operation_name = self._extract_operation_name(url, body)
                if actual_operation_name:
                    self._seen_operations.append(actual_operation_name)
                if self._operation_matches(actual_operation_name, operation_name) and "operationName=" in url:
                    headers = request["headers"]
                    client_token, authorization, persisted_query = self._extract_auth(url, headers)
                    if client_token and authorization and persisted_query:
                        self.spotify_request_headers = headers
                        self.operation_names[operation_name] = actual_operation_name or operation_name
                        return client_token, authorization, persisted_query
                if self._operation_matches(actual_operation_name, operation_name) and body:
                    headers = request["headers"]
                    client_token, authorization, persisted_query = self._extract_auth_from_body(body, headers)
                    if client_token and authorization and persisted_query:
                        self.spotify_request_headers = headers
                        self.operation_names[operation_name] = actual_operation_name or operation_name
                        return client_token, authorization, persisted_query
        if raise_on_missing:
            print(f"Could not find the {operation_name} request in the network logs.")
        if raise_on_missing and operation_name == "libraryV3":
            raise Exception("Could not fetch the library data. "\
                            "Make sure you are logged in to Spotify "\
                            "and the language is set to English.")
            
        return None

    def _wait_for_auth_from_network_logs(self, operation_name, timeout=10):
        end_time = time.time() + timeout
        while time.time() < end_time:
            auth = self._extract_auth_from_network_logs(operation_name, raise_on_missing=False)
            if auth:
                return auth
            time.sleep(0.5)
        return None

    def _click_library_button(self):
        selectors = [
            '[aria-label="Open Your Library"]',
            '[aria-label="Collapse Your Library"]',
            '[aria-label="Your Library"]',
            '[data-testid="your-library-link"]',
            '[data-testid="rootlist-button"]',
            'a[href="/collection"]',
            'a[href="/collection/playlists"]',
        ]

        for selector in selectors:
            try:
                element = WebDriverWait(self.driver, 3).until(
                    EC.element_to_be_clickable((By.CSS_SELECTOR, selector))
                )
                element.click()
                return True
            except (TimeoutException, NoSuchElementException):
                continue

        return self.driver.execute_script("""
            const candidates = [...document.querySelectorAll('a,button')];
            const libraryNode = candidates.find((node) => {
                const label = [
                    node.getAttribute('aria-label'),
                    node.getAttribute('title'),
                    node.textContent
                ].filter(Boolean).join(' ').toLowerCase();
                return label.includes('library');
            });
            if (!libraryNode) return false;
            libraryNode.click();
            return true;
        """)

    
    def _get_library_auth(self):
        # turning on network logs
        self.driver.execute_cdp_cmd("Network.enable", {})
        # we go back to homepage
        self._safe_get('https://open.spotify.com')

        auth = self._wait_for_auth_from_network_logs('libraryV3', timeout=5)
        if auth:
            return auth

        if self._click_library_button():
            auth = self._wait_for_auth_from_network_logs('libraryV3', timeout=10)
            if auth:
                return auth

        for url in (
            'https://open.spotify.com/collection',
            'https://open.spotify.com/collection/playlists',
            'https://open.spotify.com/collection/tracks',
            'https://open.spotify.com/collection/albums',
            'https://open.spotify.com/collection/artists',
        ):
            self._safe_get(url)
            auth = self._wait_for_auth_from_network_logs('libraryV3', timeout=10)
            if auth:
                return auth

        print("Could not find the library button or capture the library request.")
        self._print_spotify_debug_info()
        return self._extract_auth_from_network_logs('libraryV3')


    def _spotify_headers(self, authorization, client_token):
        headers = {
            'accept': 'application/json',
            'authorization': authorization,
            'client-token': client_token,
            'content-type': 'application/json;charset=UTF-8'
        }
        headers.update({
            key: value
            for key, value in self.spotify_request_headers.items()
            if key.lower() not in {'host', 'content-length'}
        })
        headers['authorization'] = authorization
        headers['client-token'] = client_token
        headers['content-type'] = 'application/json;charset=UTF-8'
        return headers

    def _library_params(self, persisted_query, limit, filters=None):
        variables = {
            "filters": filters or [],
            "order": None,
            "textFilter": "",
            "features": ["LIKED_SONGS", "YOUR_EPISODES"],
            "limit": limit,
            "offset": 0,
            "flatten": False,
            "expandedFolders": [],
            "folderUri": None,
            "includeFoldersWhenFlattening": True,
        }
        return {
            'operationName': 'libraryV3',
            'variables': json.dumps(variables, separators=(',', ':')),
            'extensions': persisted_query
        }

    def _add_library_item(self, item, seen_uris):
        data = item['item']['data']
        try:
            if data['__typename'] == 'PseudoPlaylist':
                self.library['HasLikedSongs'] = True
                return

            if data['__typename'] == 'NotFound':
                self.library['TrashItems'] += 1
                return

            if data['__typename'] == 'Folder':
                uri = data['uri']
                target = 'Folders'
                entry = {'name': data['name'], 'uri': uri}
            elif data['__typename'] == 'Artist':
                uri = data['uri']
                target = 'Artists'
                entry = {'name': data['profile']['name'], 'uri': uri}
            elif data['__typename'] == 'Album':
                uri = data['uri']
                target = 'Albums'
                entry = {'name': data['name'], 'uri': uri}
            elif data['__typename'] == 'Playlist':
                uri = data['uri']
                target = 'Playlists'
                entry = {'name': data['name'], 'uri': uri}
            else:
                print(f"Unsuported type: {data['__typename']}\nMore info: {item}")
                return
        except KeyError:
            raise KeyError(f"KeyError: {data['__typename']}\nMore info: {item}")

        if uri not in seen_uris:
            self.library[target].append(entry)
            seen_uris.add(uri)

    def _fetch_library_items(self, persisted_query, headers, filters=None, limit=50):
        url = 'https://api-partner.spotify.com/pathfinder/v1/query'
        params = self._library_params(persisted_query, limit, filters)
        response = requests.get(url, headers=headers, params=params)
        print('' if response.status_code == 200 else f"Error! Code: {response.status_code}")
        if response.status_code != 200:
            print(response.text[:500])
            raise Exception("Could not fetch Spotify library from api-partner.spotify.com.")

        res_j = json.loads(response.text)
        library_data = res_j['data']['me']['libraryV3']
        total_count = library_data.get('totalCount', len(library_data.get('items', [])))
        if 'items' not in library_data:
            return []
        if total_count > limit:
            params = self._library_params(
                persisted_query,
                (2 + total_count // 25) * 25,
                filters,
            )
            response = requests.get(url, headers=headers, params=params)
            if response.status_code != 200:
                print(f"Error! Code: {response.status_code}")
                print(response.text[:500])
                raise Exception("Could not fetch full Spotify library from api-partner.spotify.com.")
            res_j = json.loads(response.text)
            library_data = res_j['data']['me']['libraryV3']
        return library_data.get('items', [])

    def get_library(self):
        client_token, authorization, persisted_query = self._get_library_auth()
        headers = self._spotify_headers(authorization, client_token)
        seen_uris = set()

        for item in self._fetch_library_items(persisted_query, headers):
            self._add_library_item(item, seen_uris)

        for filters in (['PLAYLISTS'], ['ALBUMS'], ['ARTISTS'], ['FOLDERS']):
            try:
                for item in self._fetch_library_items(persisted_query, headers, filters=filters):
                    self._add_library_item(item, seen_uris)
            except Exception as error:
                print(f"Could not fetch Spotify library filter {filters}: {error}")

        print(
            "Loaded Spotify library: "
            f"{len(self.library['Albums'])} albums, "
            f"{len(self.library['Playlists'])} playlists, "
            f"{len(self.library['Artists'])} artists, "
            f"liked songs: {self.library['HasLikedSongs']}."
        )

        return client_token, authorization

    def _scroll_tracklist(self, rounds=6, pause=0.6):
        # The fetchLibraryTracks request only fires once Spotify's virtualized
        # track list actually renders/scrolls, so nudge it to trigger the call.
        # The list lives in an inner scroll container, not document.body, so we
        # try both the window and the most likely scrollable elements.
        script = """
            const targets = new Set([document.scrollingElement, document.body]);
            document.querySelectorAll('[data-overlayscrollbars-viewport],main,[role="grid"]').forEach(el => targets.add(el));
            targets.forEach(el => { if (el) el.scrollTop = el.scrollHeight; });
            window.scrollTo(0, document.body.scrollHeight);
        """
        for _ in range(rounds):
            try:
                self.driver.execute_script(script)
            except WebDriverException:
                pass
            time.sleep(pause)

    @staticmethod
    def _request_variables(url, body):
        # Returns the GraphQL `variables` dict for a request, from either the
        # URL query string (GET) or the JSON body (POST).
        try:
            if "operationName=" in url:
                raw = urllib.parse.parse_qs(urllib.parse.urlparse(url).query).get("variables", [None])[0]
                return json.loads(raw) if raw else {}
            if body:
                return json.loads(body).get("variables", {}) or {}
        except (json.JSONDecodeError, ValueError):
            return {}
        return {}

    def _poll_performance_requests(self, duration=14.0, interval=0.4, scroll=True):
        # Chrome's performance log buffer is small and overflows quickly on a
        # busy page, evicting the early Liked Songs request before we read it.
        # Poll frequently (get_log drains incrementally) so nothing is missed.
        records = []
        deadline = time.time() + duration
        scrolled = 0
        while time.time() < deadline:
            try:
                logs = self.driver.get_log("performance")
            except WebDriverException:
                logs = []
            for log in logs:
                try:
                    message = json.loads(log["message"])["message"]
                except (KeyError, json.JSONDecodeError):
                    continue
                if message.get("method") != "Network.requestWillBeSent":
                    continue
                records.append(message["params"]["request"])
            if scroll and scrolled < 8:
                self._scroll_tracklist(rounds=1, pause=0.0)
                scrolled += 1
            time.sleep(interval)
        return records

    def _capture_liked_query(self, tag):
        records = self._poll_performance_requests()

        liked_ops = {"fetchPlaylistContents", "fetchPlaylist", "fetchLibraryTracks"}
        seen = []
        found = None
        for request in records:
            url = request.get("url", "")
            body = request.get("postData", "")
            if "pathfinder" not in url and "operationName" not in url and not body:
                continue
            op = self._extract_operation_name(url, body)
            if not op:
                continue
            headers = request.get("headers", {})
            variables = self._request_variables(url, body)
            uri = variables.get("uri", "")
            has_auth = bool(self._get_header(headers, "authorization"))
            has_ct = bool(self._get_header(headers, "client-token"))
            via = "url" if "operationName=" in url else "body"
            seen.append((op, uri, has_auth, has_ct, via))
            self._seen_operations.append(op)

            # Liked Songs is the playlist whose URI is the user's collection.
            is_liked = ("collection" in uri) or (op == "fetchLibraryTracks")
            if found is None and op in liked_ops and is_liked:
                try:
                    if "operationName=" in url:
                        c_token, auth, persisted = self._extract_auth(url, headers)
                    else:
                        c_token, auth, persisted = self._extract_auth_from_body(body, headers)
                    if c_token and auth and persisted:
                        self.spotify_request_headers = headers
                        self.operation_names["LikedSongs"] = op
                        self.liked_uri = uri
                        self.liked_variables = variables
                        found = {
                            'persisted': persisted,
                            'op': op,
                            'uri': uri,
                            'variables': variables,
                        }
                except (KeyError, IndexError, json.JSONDecodeError):
                    pass

        # Collect every Spotify API/data URL so we can see exactly what loads
        # the liked tracks when no recognizable GraphQL op matches.
        api_urls = []
        seen_urls = set()
        for request in records:
            u = request.get("url", "")
            if not any(host in u for host in (
                "api-partner.spotify.com", "spclient", "gew", "api.spotify.com",
                "/pathfinder/", "/collection", "tracks",
            )):
                continue
            key = u.split("?")[0]
            if key in seen_urls:
                continue
            seen_urls.add(key)
            api_urls.append(u[:300])

        try:
            with open("setup_debug.log", "a") as f:
                f.write(f"=== {tag} | url={self.driver.current_url} | reqs={len(records)} | matched={found is not None} ===\n")
                for op, uri, has_auth, has_ct, via in seen:
                    f.write(f"  op={op} uri={uri!r} auth={has_auth} client_token={has_ct} via={via}\n")
                if not seen:
                    f.write("  (no GraphQL operations captured)\n")
                f.write("  --- unique API URLs ---\n")
                for u in api_urls:
                    f.write(f"  URL {u}\n")
                if found:
                    f.write(f"  --> captured liked op={found['op']} uri={found['uri']!r}\n")
        except OSError:
            pass
        return found

    # The web player serves Liked Songs from a local (websocket-synced) cache,
    # so there's no HTTP request to capture/replay and the public Web API is
    # hard rate-limited for this token. Instead we read the rendered track rows
    # straight from the DOM and persist them to the cache the app reads.
    _LIKED_EXTRACT_JS = """
        const rows = document.querySelectorAll('div[role="row"][aria-rowindex]');
        const out = [];
        rows.forEach(r => {
            const link = r.querySelector('[data-testid="internal-track-link"]')
                      || r.querySelector('a[href*="/track/"]');
            if (!link) return;  // skip the header row and non-track rows
            const title = link.textContent.trim();
            const artists = Array.from(r.querySelectorAll('a[href*="/artist/"]'))
                .map(a => a.textContent.trim()).filter(Boolean).join(', ');
            if (title) out.push([r.getAttribute('aria-rowindex'), title, artists]);
        });
        return out;
    """
    # Virtualized list: scroll the last rendered row into view to force the
    # next batch to render, and also push the scroll container to the bottom.
    _LIKED_SCROLL_JS = """
        function scrollable(el){
            while (el) {
                const s = getComputedStyle(el);
                if (/(auto|scroll)/.test(s.overflowY) && el.scrollHeight > el.clientHeight + 4) return el;
                el = el.parentElement;
            }
            return null;
        }
        const rows = document.querySelectorAll('div[role="row"][aria-rowindex]');
        if (!rows.length) return 0;
        const last = rows[rows.length - 1];
        last.scrollIntoView(false);
        const sc = scrollable(last);
        if (sc) sc.scrollTop = sc.scrollTop + sc.clientHeight;
        return rows.length;
    """

    def _scrape_liked_songs(self):
        self._safe_get('https://open.spotify.com/collection/tracks')
        try:
            WebDriverWait(self.driver, 20).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, 'div[role="row"][aria-rowindex]'))
            )
        except TimeoutException:
            print("Liked Songs track rows never rendered; cannot scrape.")
            return False

        collected = {}
        last_count = -1
        stable = 0
        for _ in range(600):
            try:
                rows = self.driver.execute_script(self._LIKED_EXTRACT_JS) or []
            except WebDriverException:
                rows = []
            for idx, title, artists in rows:
                try:
                    collected[int(idx)] = (title, artists)
                except (TypeError, ValueError):
                    continue

            try:
                self.driver.execute_script(self._LIKED_SCROLL_JS)
            except WebDriverException:
                pass
            time.sleep(0.5)

            cur_count = len(collected)
            if cur_count <= last_count:
                stable += 1
                if stable >= 8:
                    break
            else:
                stable = 0
                last_count = cur_count

        items = [collected[k] for k in sorted(collected)]
        try:
            with open("setup_debug.log", "a") as f:
                f.write(f"=== liked scrape | rows={len(items)} ===\n")
        except OSError:
            pass

        if not items:
            return False

        self.liked_songs = items
        try:
            with open("liked_cache.json", "w") as f:
                json.dump({"items": [list(pair) for pair in items]}, f)
        except OSError:
            pass
        return True

    def _get_persisted_playlists(self):
        self._safe_get('https://open.spotify.com/playlist/3QqoFD4Y4XaLoQBYkh2cAj')
        self.driver.implicitly_wait(3)

        try:
            WebDriverWait(self.driver, 7).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, '[data-testid="play-button"]'))
            )
        except TimeoutException:
            print("woopsies")

        auth = (
            self._extract_auth_from_network_logs('fetchPlaylist', raise_on_missing=False) or
            self._extract_auth_from_network_logs('fetchPlaylistWithGatedEntityRelations', raise_on_missing=False)
        )
        if not auth:
            print("Could not capture the playlist query. Playlists may be unavailable this run.")
            return False

        client_token, authorization, persisted_query = auth
        if client_token and authorization and persisted_query:
            self.persisted_qs['Playlists'] = persisted_query
            return True
        
        return False

    def _get_persisted_albums(self):
        self._safe_get('https://open.spotify.com/album/19WTqbdqDMWMthZfkmxSbx')
        self.driver.implicitly_wait(3)
        
        try:
            WebDriverWait(self.driver, 7).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, '[data-testid="play-button"]'))
            )
        except TimeoutException:
            print("woopsies")

        auth = self._extract_auth_from_network_logs('getAlbum', raise_on_missing=False)
        if not auth:
            print("Could not capture the album query. Albums may be unavailable this run.")
            return False

        client_token, authorization, persisted_query = auth
        if client_token and authorization and persisted_query:
            self.persisted_qs['Albums'] = persisted_query
            return True
        return False        

    def _get_persisted_artists(self):
        self._safe_get('https://open.spotify.com/artist/483Rl4WY6iIJ9czOrOgymb')
        self.driver.implicitly_wait(3)

        try:
            WebDriverWait(self.driver, 7).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, '[data-testid="play-button"]'))
            )
        except TimeoutException:
            print("woopsies")

        auth = self._extract_auth_from_network_logs('queryArtistOverview', raise_on_missing=False)
        if not auth:
            print("Could not capture the artist query. Artists may be unavailable this run.")
            return False

        client_token, authorization, persisted_query = auth
        if client_token and authorization and persisted_query:
            self.persisted_qs['Artists'] = persisted_query
            return True
        return False

    def get_persist_queries(self):
        if not self.has_p_keys:
            if self.library['Albums']:
                self._get_persisted_albums()
            if self.library['Artists']:
                self._get_persisted_artists()
            if self.library['Playlists']:
                self._get_persisted_playlists()
            self.has_p_keys = True

    def _login_ytm(self):
        self._safe_get("https://music.youtube.com/")
         
        # check if already logged in:
        logged_in = False
        try:
            WebDriverWait(self.driver,5).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, '.settings-button'))
            )
            logged_in = True
        except TimeoutException:
            try:
                self.driver.find_element(By.CSS_SELECTOR, '.sign-in-link')
            except:
                raise "Network Error"
        if not logged_in:
            user_confirm = False
            requests.get("http://localhost:5001/update_login?status=false&type=ytm")
            while not user_confirm:
                time.sleep(1)
                res = requests.get("http://localhost:5001/check_user_confirmation")
                user_confirm = "true" in res.text
        requests.get("http://localhost:5001/update_login?status=true")
        return logged_in
    
    def _get_cookies(self):
        return self.driver.get_cookies()

    def _get_ytm_cookies(self):
        self._login_ytm()
        # turning on network logs again just to be sure
        self.driver.execute_cdp_cmd("Network.enable", {})
        # the driver might be already on the home page but going back to ytm just in case
        self._safe_get('https://music.youtube.com/')

        WebDriverWait(self.driver,5).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, '.settings-button'))
        )

        last_height = self.driver.execute_script("return document.body.scrollHeight")

        while True:
            self.driver.execute_script("window.scrollTo(0, document.body.scrollHeight*0.90);")

            time.sleep(0.5)

            new_height = self.driver.execute_script("return document.body.scrollHeight")
            if new_height == last_height:
                break
            last_height = new_height

        logs = self.driver.get_log("performance")

        for log in logs:
            message = json.loads(log["message"])["message"]
            if message["method"] == "Network.requestWillBeSent":
                request = message["params"]["request"]
                url = request["url"]
                # `browse?` is what we need for the cookies
                if "browse?" in url:
                    self.yt_cookies = request["headers"]
                    cookies_j = self._get_cookies()
                    extracted_c = ""
                    for cookie in cookies_j:
                        c = f"{cookie['name']}={cookie['value']}; "
                        extracted_c += c
                    self.yt_cookies['cookie'] = extracted_c[:-2]
                    return True
