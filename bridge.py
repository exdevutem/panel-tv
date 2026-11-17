#!/usr/bin/env python3
"""
Puente del panel ExDev (solo biblioteca estándar de Python 3.9+).

Qué hace
  - Sirve panel.html en  http://<este-equipo>:8080/
  - Entrega todos los datos del panel en  GET /api/panel  (JSON)
  - Guarda los tokens (Home Assistant, Notion) en el servidor: nunca viajan al navegador.

Fuentes
  Música ........ Home Assistant: entidad media_player
  Calendario .... Home Assistant: entidades calendar.* (p. ej. integración Google Calendar)
  Contador ...... el proyecto de visión hace  POST /api/room  {"count": 4}
                  (o bien una entidad de Home Assistant, ver HA_ROOM_ENTITY)
  Cumpleaños y últimos miembros ... base "Directorio" de Notion (API oficial)

Privacidad: de Notion solo se leen Nombre, Cumpleaños, Puesto de trabajo, Organización y la fecha de
creación de la fila. RUT, teléfono, correo, NFC y demás campos nunca se piden ni se muestran.

Configuración (variables de entorno)
  HA_URL            http://homeassistant.local:8123
  HA_TOKEN          token de acceso de larga duración de Home Assistant
  HA_MEDIA_PLAYER   p. ej. media_player.sala_exdev
  HA_CALENDARS      p. ej. calendar.eventos,calendar.exdev   (separadas por coma)
  HA_ROOM_ENTITY    (opcional) p. ej. sensor.personas_en_sala
  NOTION_TOKEN      token de una integración de Notion con acceso al Directorio
  NOTION_DATA_SOURCE  por defecto: 0484b0f2-9ac5-4edb-ab1c-bbfffaced2c8 (Directorio)
  PANEL_KEY         (opcional) clave que debe enviar quien haga POST /api/room (cabecera X-Panel-Key)
  LIVE_MIN_SECONDS  (opcional) duración sobre la cual se considera "en vivo" (por defecto 43200 = 12 h)
  PORT              por defecto 8080
  TZ_NAME           por defecto America/Santiago

Uso:  HA_TOKEN=... NOTION_TOKEN=... python3 bridge.py
"""
import json, os, sys, threading, time, urllib.error, urllib.parse, urllib.request
from datetime import datetime, timedelta, timezone, date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
HA_URL = os.environ.get('HA_URL', 'http://homeassistant.local:8123').rstrip('/')
HA_TOKEN = os.environ.get('HA_TOKEN', '')
HA_MEDIA_PLAYER = os.environ.get('HA_MEDIA_PLAYER', '')
HA_CALENDARS = [c.strip() for c in os.environ.get('HA_CALENDARS', '').split(',') if c.strip()]
HA_ROOM_ENTITY = os.environ.get('HA_ROOM_ENTITY', '')
NOTION_TOKEN = os.environ.get('NOTION_TOKEN', '')
NOTION_DS = os.environ.get('NOTION_DATA_SOURCE', '0484b0f2-9ac5-4edb-ab1c-bbfffaced2c8')
PANEL_KEY = os.environ.get('PANEL_KEY', '')
LIVE_MIN_SECONDS = int(os.environ.get('LIVE_MIN_SECONDS', '43200'))   # duración mayor a esto = transmisión en vivo
PORT = int(os.environ.get('PORT', '8080'))
TZ = ZoneInfo(os.environ.get('TZ_NAME', 'America/Santiago'))
NOTION_ORG = '✨ ExDev ✨'

ROOM = {'count': None, 'ts': None}          # último valor recibido por POST /api/room
_cache = {}                                  # clave -> (expira, valor)
_lock = threading.Lock()


def cached(key, ttl, fn):
    now = time.time()
    with _lock:
        hit = _cache.get(key)
        if hit and hit[0] > now:
            return hit[1]
    try:
        value = fn()
    except Exception:
        with _lock:                           # si falla, se entrega lo último conocido (si existe)
            hit = _cache.get(key)
        if hit:
            return hit[1]
        raise
    with _lock:
        _cache[key] = (now + ttl, value)
    return value


# ----------------------------- Home Assistant -----------------------------
def ha_get(path, raw=False):
    req = urllib.request.Request(HA_URL + path, headers={'Authorization': 'Bearer ' + HA_TOKEN})
    with urllib.request.urlopen(req, timeout=8) as r:
        data = r.read()
        return (data, r.headers.get('Content-Type', 'application/octet-stream')) if raw else json.loads(data)


def get_music():
    if not (HA_TOKEN and HA_MEDIA_PLAYER):
        return 'off', None
    try:
        s = ha_get('/api/states/' + urllib.parse.quote(HA_MEDIA_PLAYER))
    except Exception as e:
        return 'error', str(e)
    a = s.get('attributes', {})
    pic = a.get('entity_picture')
    if not pic:
        art = None
    elif pic.startswith(('http://', 'https://')):
        art = pic          # imagen remota (p. ej. miniatura de YouTube): el navegador la carga directo
    else:
        art = '/api/art?v=' + urllib.parse.quote(str(a.get('media_title', '')))   # ruta local de HA: se sirve por el puente
    duration, position = a.get('media_duration'), a.get('media_position')
    # Los directos de YouTube reportan duración/posición absurdas (días); se marcan como "en vivo" y se descartan
    live = bool(duration) and float(duration) > LIVE_MIN_SECONDS
    if live:
        duration = position = None
    return 'ok', {
        'state': s.get('state'),
        'title': a.get('media_title'), 'artist': a.get('media_artist') or a.get('media_album_artist'),
        'album': a.get('media_album_name'), 'art': art, 'live': live,
        'duration': duration, 'position': position,
        'position_at': a.get('media_position_updated_at'),
    }


def get_room():
    if HA_ROOM_ENTITY and HA_TOKEN:
        try:
            s = ha_get('/api/states/' + urllib.parse.quote(HA_ROOM_ENTITY))
            return 'ok', {'count': int(float(s['state'])), 'ts': s.get('last_updated')}
        except Exception as e:
            return 'error', str(e)
    if ROOM['ts'] is None:
        return 'off', None
    return 'ok', {'count': ROOM['count'], 'ts': ROOM['ts']}


def get_events():
    if not (HA_TOKEN and HA_CALENDARS):
        return 'off', None
    now = datetime.now(TZ)
    q = urllib.parse.urlencode({'start': now.isoformat(), 'end': (now + timedelta(days=45)).isoformat()})
    out, failed = [], 0
    for cal in HA_CALENDARS:
        try:
            for e in ha_get(f'/api/calendars/{urllib.parse.quote(cal)}?{q}'):
                st, en = e.get('start', {}), e.get('end', {})
                all_day = 'date' in st
                start = st.get('dateTime') or st.get('date')
                end = en.get('dateTime') or en.get('date')
                if all_day and end:                      # en HA el fin de un evento de día completo es exclusivo
                    end = (date.fromisoformat(end) - timedelta(days=1)).isoformat()
                out.append({'title': e.get('summary'), 'start': start, 'end': end,
                            'all_day': all_day, 'location': e.get('location') or None})
        except Exception:
            failed += 1
    if failed == len(HA_CALENDARS):
        return 'error', 'no se pudo leer ningún calendario'
    def key(e):
        d = datetime.fromisoformat(e['start'])
        return d if d.tzinfo else d.replace(tzinfo=TZ)
    out.sort(key=key)
    seen, uniq = set(), []                               # evita duplicados entre calendarios
    for e in out:
        k = (e['title'], e['start'])
        if k not in seen:
            seen.add(k); uniq.append(e)
    return 'ok', uniq[:12]


# ----------------------------- Notion -----------------------------
def notion_query():
    rows, cursor = [], None
    while True:
        body = {'page_size': 100, 'filter': {'property': 'Organización', 'multi_select': {'contains': NOTION_ORG}}}
        if cursor:
            body['start_cursor'] = cursor
        req = urllib.request.Request(
            f'https://api.notion.com/v1/data_sources/{NOTION_DS}/query',
            data=json.dumps(body).encode(), method='POST',
            headers={'Authorization': 'Bearer ' + NOTION_TOKEN, 'Notion-Version': '2025-09-03',
                     'Content-Type': 'application/json'})
        with urllib.request.urlopen(req, timeout=15) as r:
            page = json.loads(r.read())
        rows += page['results']
        if not page.get('has_more'):
            return rows
        cursor = page['next_cursor']


def short_name(full):
    """Nombre + apellido paterno: 'Nicolás Leonardo Soto Mardones' -> 'Nicolás Soto'."""
    t = (full or '').split()
    if len(t) <= 2:
        return ' '.join(t)
    return f'{t[0]} {t[-2]}'


def parse_notion(rows, today):
    birthdays, members = [], []
    for p in rows:
        pr = p.get('properties', {})
        title = ''.join(x.get('plain_text', '') for x in pr.get('Nombre', {}).get('title', []))
        roles = [o['name'] for o in pr.get('Puesto de trabajo', {}).get('multi_select', [])]
        if not title or 'Inactivo' in roles:
            continue
        name = short_name(title)
        bd = (pr.get('Cumpleaños', {}).get('date') or {}).get('start')
        if bd:
            try:
                b = date.fromisoformat(bd[:10])
                try:
                    nxt = b.replace(year=today.year)
                except ValueError:                        # 29 de febrero en año no bisiesto
                    nxt = date(today.year, 2, 28)
                if nxt < today:
                    try:
                        nxt = b.replace(year=today.year + 1)
                    except ValueError:
                        nxt = date(today.year + 1, 2, 28)
                birthdays.append({'name': name, 'month': b.month, 'day': b.day, 'days': (nxt - today).days})
            except ValueError:
                pass
        members.append({'name': name, 'role': roles[0] if roles else 'Miembro', 'joined': p.get('created_time')})
    birthdays.sort(key=lambda x: x['days'])
    members.sort(key=lambda x: x['joined'] or '', reverse=True)
    return birthdays[:8], members[:8]


def get_notion():
    if not NOTION_TOKEN:
        return 'off', None
    try:
        rows = cached('notion', 600, notion_query)
    except Exception as e:
        return 'error', str(e)
    return 'ok', parse_notion(rows, datetime.now(TZ).date())


# ----------------------------- HTTP -----------------------------
def panel_payload():
    sources, out = {}, {'ts': datetime.now(timezone.utc).isoformat()}
    sources['music'], out['music'] = get_music()
    sources['room'], out['room'] = get_room()
    sources['calendar'], out['events'] = cached('events', 60, get_events) if (HA_TOKEN and HA_CALENDARS) else ('off', None)
    sources['notion'], n = get_notion()
    out['birthdays'], out['members'] = n if isinstance(n, tuple) else (None, None)
    out['sources'] = sources
    return out


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype='application/json; charset=utf-8'):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path in ('/', '/index.html', '/panel.html'):
            return self._send(200, open(os.path.join(HERE, 'panel.html'), 'rb').read(), 'text/html; charset=utf-8')
        if path == '/api/panel':
            return self._send(200, panel_payload())
        if path == '/api/art' and HA_TOKEN and HA_MEDIA_PLAYER:
            try:
                st = ha_get('/api/states/' + urllib.parse.quote(HA_MEDIA_PLAYER))
                pic = st['attributes']['entity_picture']
                data, ctype = ha_get(pic if pic.startswith('/') else urllib.parse.urlparse(pic).path, raw=True)
                return self._send(200, data, ctype)
            except Exception:
                return self._send(404, b'', 'text/plain')
        self._send(404, {'error': 'not found'})

    def do_POST(self):
        if urllib.parse.urlparse(self.path).path != '/api/room':
            return self._send(404, {'error': 'not found'})
        if PANEL_KEY and self.headers.get('X-Panel-Key') != PANEL_KEY:
            return self._send(401, {'error': 'clave inválida'})
        try:
            n = int(self.headers.get('Content-Length', '0'))
            count = int(json.loads(self.rfile.read(n))['count'])
            if count < 0 or count > 999:
                raise ValueError
        except Exception:
            return self._send(400, {'error': 'se espera {"count": <entero>}'})
        ROOM['count'], ROOM['ts'] = count, datetime.now(timezone.utc).isoformat()
        self._send(200, {'ok': True, 'count': count})


if __name__ == '__main__':
    print(f'Panel ExDev en http://0.0.0.0:{PORT}/  (música: {bool(HA_MEDIA_PLAYER)}, calendarios: {len(HA_CALENDARS)}, notion: {bool(NOTION_TOKEN)})', file=sys.stderr)
    ThreadingHTTPServer(('0.0.0.0', PORT), Handler).serve_forever()
