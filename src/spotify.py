import requests
import json 
import os 
import time
from src.setup import SetupManager

class SpotifyManager:
    CACHE_VERSION = 5
    LIKED_RATE_LIMIT_COOLDOWN = 30
    LIKED_CACHE_FILE = 'liked_cache.json'

    def __init__(self):
        self.liked_uri = ''
        self.liked_variables = {}
        if os.path.exists('spotify_auth.json'):
            with open('spotify_auth.json', 'r') as f:
                saved_data = json.load(f)
            if time.time() > saved_data['expires']:
                os.remove('spotify_auth.json')
            elif saved_data.get('cache_version') != self.CACHE_VERSION:
                os.remove('spotify_auth.json')
            elif not saved_data.get('spotify_request_headers'):
                os.remove('spotify_auth.json')
            else:
                self.authorization = saved_data['authorization']
                self.client_token = saved_data['client_token']
                self.persisted_queries = saved_data['persisted_queries']
                self.operation_names = saved_data.get('operation_names', {})
                self.library = saved_data['library']
                self.spotify_request_headers = saved_data.get('spotify_request_headers', {})
                self.liked_uri = saved_data.get('liked_uri', '')
                self.liked_variables = saved_data.get('liked_variables', {})
                self.session = None

        self._liked_cache = None
        self._liked_retry_after = 0
        # Resumable pagination state so a rate-limited fetch can continue
        # from where it stopped instead of restarting from offset 0.
        self._liked_partial_items = []
        self._liked_offset = 0
        self._load_liked_disk_cache()

        if not os.path.exists('spotify_auth.json'):
            self.session = SetupManager()
            self.client_token, self.authorization = self.session.get_library()
            self.library = self.session.library
            self.spotify_request_headers = self.session.spotify_request_headers
            if not self.session.has_p_keys:
                self.session.get_persist_queries()
            self.operation_names = self.session.operation_names
            self.persisted_queries = self.session.persisted_qs
            self.liked_uri = getattr(self.session, 'liked_uri', '')
            self.liked_variables = getattr(self.session, 'liked_variables', {})
            self._save_auth_file()
        
        requests.get("http://localhost:5001/initialized")

    def _save_auth_file(self):
        try:
            with open('spotify_auth.json', 'w') as f:
                json.dump({
                   'client_token'       :   self.client_token,
                   'authorization'      :   self.authorization,
                   'library'            :   self.library,
                   'persisted_queries'  :   self.persisted_queries,
                   'operation_names'    :   self.operation_names,
                   'spotify_request_headers' : self.spotify_request_headers,
                   'liked_uri'          :   self.liked_uri,
                   'liked_variables'    :   self.liked_variables,
                   'cache_version'      :   self.CACHE_VERSION,
                   'expires'            :   time.time() + 24*60*60
                }, f)
        except OSError:
            pass

    def refresh_library(self):
        try:
            session = getattr(self, 'session', None) or SetupManager()
            self.session = session
            self.client_token, self.authorization = session.get_library()
            self.library = session.library
            self._save_auth_file()
            return True, "Library refreshed!"
        except Exception as e:
            print(f"Error refreshing library: {e}")
            return False, str(e)

    def reauthenticate(self):
        try:
            if os.path.exists('spotify_auth.json'):
                try:
                    os.remove('spotify_auth.json')
                except OSError:
                    pass
            session = SetupManager()
            self.session = session
            self.client_token, self.authorization = session.get_library()
            self.library = session.library
            self.spotify_request_headers = getattr(session, 'spotify_request_headers', {})
            self.operation_names = getattr(session, 'operation_names', {})
            self.persisted_queries = getattr(session, 'persisted_qs', {})
            self._save_auth_file()
            return True
        except Exception as e:
            print(f"Reauthentication failed: {e}")
            return False

    def _get_res_from_spot(self, operation, persisted, uri=None, limit=100, offset=0, retried_401=False):
        if not persisted:
            return f"missing persisted query for {operation}", False

        variables = {
            "uri" : uri if uri else "",
            "locale":"",
            "offset": offset,
            "limit": limit,
            "enableWatchFeedEntrypoint": False if operation == "fetchPlaylist" else "",
        }
        if variables['uri'] == "":
            del variables['uri']
        endpoint = 'https://api-partner.spotify.com/pathfinder/v1/query'
        params = {
            'operationName': f'{operation}',
            'variables': json.dumps(variables),
            'extensions': persisted
        }
        headers = {
            'accept': 'application/json',
            'authorization': self.authorization,
            'client-token': self.client_token,
            'content-type': 'application/json;charset=UTF-8'
        }
        headers.update({
            key: value
            for key, value in self.spotify_request_headers.items()
            if key.lower() not in {'host', 'content-length'}
        })
        headers['authorization'] = self.authorization
        headers['client-token'] = self.client_token
        headers['content-type'] = 'application/json;charset=UTF-8'
        response = requests.get(endpoint, headers=headers, params=params)
        if response.status_code == 200:
            res_j = json.loads(response.text)
            return res_j, True
        if response.status_code == 401 and not retried_401:
            print("Spotify token expired (401). Re-authenticating automatically...")
            if self.reauthenticate():
                return self._get_res_from_spot(operation, persisted, uri, limit, offset, retried_401=True)
        return response.status_code, False
    
    @staticmethod
    def _extract_from_trackv2(tracks):
        extracted = []
        for track in tracks:
            track = track['track'] if 'track' in track else track['data']
            if 'name' not in track or track['name'].strip() == '': continue
            artists = "".join([","+artist['profile']['name'] for artist in track['artists']['items']]) if 'artists' in track else ""
            extracted.append((track['name'],artists[1:]))
        return extracted

    def get_playlist(self, uri, batch_size=100):
        offset = 0
        all_tracks = []
        while True:
            res_j, success = self._get_res_from_spot('fetchPlaylist', self.persisted_queries['Playlists'], uri, limit=batch_size, offset=offset)
            if not success:
                if all_tracks:
                    return all_tracks, True
                return res_j, False
            content = res_j.get('data', {}).get('playlistV2', {}).get('content', {})
            total_count = int(content.get('totalCount', 0))
            items = content.get('items', [])
            fixed_tracks = [item['itemV2'] for item in items if isinstance(item, dict) and 'itemV2' in item]
            extracted = self._extract_from_trackv2(fixed_tracks)
            all_tracks.extend(extracted)
            offset += len(items)
            if offset >= total_count or not items:
                break
        return all_tracks, True
    
    def get_artists(self, uri):
        # currently only choosing the topTracks
        res_j, success = self._get_res_from_spot('queryArtistOverview', self.persisted_queries['Artists'], uri)
        if success:
            top_tracks = res_j['data']['artistUnion']['discography']['topTracks']
            extracted = self._extract_from_trackv2(top_tracks['items'])
            return extracted, success
        return res_j, success

    def get_albums(self, uri, batch_size=100):
        offset = 0
        all_tracks = []
        while True:
            res_j, success = self._get_res_from_spot('getAlbum', self.persisted_queries['Albums'], uri, limit=batch_size, offset=offset)
            if not success:
                if all_tracks:
                    return all_tracks, True
                return res_j, False
            tracks_v2 = res_j.get('data', {}).get('albumUnion', {}).get('tracksV2', {})
            total_count = int(tracks_v2.get('totalCount', 0))
            items = tracks_v2.get('items', [])
            extracted = self._extract_from_trackv2(items)
            all_tracks.extend(extracted)
            offset += len(items)
            if offset >= total_count or not items:
                break
        return all_tracks, True
    
    def get_liked(self, limit=50):
        if self._liked_cache is None:
            self._load_liked_disk_cache()
        if self._liked_cache is not None:
            return self._liked_cache, True

        if time.time() < self._liked_retry_after:
            wait_seconds = int(self._liked_retry_after - time.time()) + 1
            return f"rate limited, try again in {wait_seconds}s", False

        # Modern Spotify exposes Liked Songs as a playlist (collection URI)
        # fetched via fetchPlaylistContents, so prefer that when captured.
        if self.persisted_queries.get('LikedSongs') and self.liked_uri:
            res, success = self._get_liked_via_playlist(limit)
            if success:
                return res, success

        res, success = self._get_liked_from_web_api()
        if success:
            return res, success

        # On-demand fallback scrape if API/GraphQL fails and no cache exists
        try:
            session = getattr(self, 'session', None)
            if session and session._scrape_liked_songs():
                self._load_liked_disk_cache()
                if self._liked_cache:
                    return self._liked_cache, True
        except Exception as e:
            print(f"On-demand liked songs scrape failed: {e}")

        return res, False

    def _get_liked_via_playlist(self, batch_size=100):
        operation = self.operation_names.get('LikedSongs', 'fetchPlaylistContents')
        endpoint = 'https://api-partner.spotify.com/pathfinder/v1/query'
        headers = {
            'accept': 'application/json',
            'authorization': self.authorization,
            'client-token': self.client_token,
            'content-type': 'application/json;charset=UTF-8',
        }
        headers.update({
            key: value
            for key, value in self.spotify_request_headers.items()
            if key.lower() not in {'host', 'content-length'}
        })
        headers['authorization'] = self.authorization
        headers['client-token'] = self.client_token
        headers['content-type'] = 'application/json;charset=UTF-8'

        offset = 0
        all_tracks = []

        while True:
            variables = dict(self.liked_variables or {})
            variables['uri'] = self.liked_uri
            variables['offset'] = offset
            variables['limit'] = batch_size

            params = {
                'operationName': operation,
                'variables': json.dumps(variables),
                'extensions': self.persisted_queries['LikedSongs'],
            }

            response = requests.get(endpoint, headers=headers, params=params)
            if response.status_code == 429:
                retry_after = max(
                    int(response.headers.get('Retry-After', '1')),
                    self.LIKED_RATE_LIMIT_COOLDOWN,
                )
                self._liked_retry_after = time.time() + retry_after
                if all_tracks:
                    break
                return f"rate limited, try again in {retry_after}s", False
            if response.status_code != 200:
                if all_tracks:
                    break
                return response.status_code, False

            res_j = json.loads(response.text)
            content = res_j.get('data', {}).get('playlistV2', {}).get('content', {})
            total_count = int(content.get('totalCount', 0))
            items = content.get('items', [])
            fixed_tracks = [item['itemV2'] for item in items if isinstance(item, dict) and 'itemV2' in item]
            extracted = self._extract_from_trackv2(fixed_tracks)
            all_tracks.extend(extracted)
            offset += len(items)
            if offset >= total_count or not items:
                break

        self._liked_cache = all_tracks
        self._save_liked_disk_cache(all_tracks)
        return all_tracks, True

    def _get_liked_from_web_api(self, limit=50):
        headers = {
            'accept': 'application/json',
            'authorization': self.authorization,
            'content-type': 'application/json;charset=UTF-8'
        }
        # Resume from any progress made before a previous rate limit.
        items = self._liked_partial_items
        offset = self._liked_offset

        while True:
            response = requests.get(
                'https://api.spotify.com/v1/me/tracks',
                headers=headers,
                params={'limit': limit, 'offset': offset},
            )
            if response.status_code == 429:
                retry_after = max(
                    int(response.headers.get('Retry-After', '1')),
                    self.LIKED_RATE_LIMIT_COOLDOWN,
                )
                # Keep what we already fetched and resume here next time.
                self._liked_partial_items = items
                self._liked_offset = offset
                self._liked_retry_after = time.time() + retry_after
                return f"rate limited, try again in {retry_after}s", False
            if response.status_code != 200:
                return response.status_code, False

            data = response.json()
            items.extend(data.get('items', []))
            total = data.get('total', len(items))
            offset += limit
            if offset >= total or not data.get('items'):
                break
            time.sleep(2)

        extracted = []
        for item in items:
            track = item.get('track') or {}
            name = track.get('name', '').strip()
            if not name:
                continue
            artists = ", ".join(
                artist.get('name', '')
                for artist in track.get('artists', [])
                if artist.get('name')
            )
            extracted.append((name, artists))
        self._liked_cache = extracted
        self._liked_partial_items = []
        self._liked_offset = 0
        self._save_liked_disk_cache(extracted)
        return extracted, True

    def _load_liked_disk_cache(self):
        if not os.path.exists(self.LIKED_CACHE_FILE):
            return
        try:
            with open(self.LIKED_CACHE_FILE, 'r') as f:
                payload = json.load(f)
            items = payload.get('items', [])
            if items:
                self._liked_cache = [tuple(pair) for pair in items]
        except (json.JSONDecodeError, OSError, TypeError):
            self._liked_cache = None

    def _save_liked_disk_cache(self, extracted):
        try:
            with open(self.LIKED_CACHE_FILE, 'w') as f:
                json.dump({
                    'cache_version': self.CACHE_VERSION,
                    'items': [list(pair) for pair in extracted],
                }, f)
        except OSError:
            pass
