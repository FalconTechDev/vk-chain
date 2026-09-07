#!/usr/bin/env python3
"""
VK Chain-of-Handshakes & Relatives/Surnames Finder.
Поддерживает поиск цепочек, родственников/однофамильцев с обработкой латиницы/кириллицы,
гибким поиском по корню, экспорт в HTML, кэширование и глубокий рекурсивный поиск (--deep).
"""

import json
import sys
import time
import signal
import argparse
import threading
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests
from datetime import datetime
from pathlib import Path

API = "https://api.vk.com/method"
V = "5.131"
LOG_FILE = Path("vk_chain_log.txt")
TOKEN_FILE = Path.home() / ".vk_chain_token"
FRIENDS_CACHE_FILE = Path("friends_cache.json")
RELATIVES_CACHE_FILE = Path("relatives_cache.json")
PHOTOS_DIR = Path("photos")
DELAY = 0.35

RELATIVE_TYPES = {
    "child": "Ребёнок",
    "sibling": "Брат / Сестра",
    "parent": "Родитель",
    "grandparent": "Дедушка / Бабушка",
    "grandchild": "Внук / Внучка"
}

TRANSLIT_MAP = {
    'a': 'а', 'b': 'б', 'v': 'в', 'g': 'г', 'd': 'д', 'e': 'е', 'yo': 'ё', 'zh': 'ж',
    'z': 'з', 'i': 'и', 'y': 'й', 'k': 'к', 'l': 'л', 'm': 'м', 'n': 'н', 'o': 'о',
    'p': 'п', 'r': 'р', 's': 'с', 't': 'т', 'u': 'у', 'f': 'ф', 'h': 'х', 'kh': 'х',
    'ts': 'ц', 'ch': 'ч', 'sh': 'ш', 'shch': 'щ', 'yu': 'ю', 'ya': 'я', 'ck': 'к', 'x': 'кс',
    'iy': 'ий', 'ij': 'ий', 'y': 'й'
}

_friends_cache: dict[int, list[int]] = {}
_relatives_cache: dict[int, list[int]] = {}
_rate_lock = threading.Lock()
_session = requests.Session()
_last = 0.0


def normalize_word(word: str) -> str:
    """Приводит слово к нижнему регистру и кириллице."""
    if not word:
        return ""
    s = word.lower().strip()
    for lat, cyr in sorted(TRANSLIT_MAP.items(), key=lambda x: len(x[0]), reverse=True):
        s = s.replace(lat, cyr)
    return s


def get_surname_stem(surname: str) -> str:
    """Очищает окончания для получения основы фамилии."""
    s = normalize_word(surname)
    if len(s) <= 3:
        return s
    
    endings = ('ова', 'ева', 'ина', 'ая', 'ов', 'ев', 'ин', 'ий', 'ый', 'а', 'я')
    for end in endings:
        if s.endswith(end):
            return s[:-len(end)]
    return s


def is_stem_match(stem1: str, stem2: str) -> bool:
    """Проверяет совпадение точное или по общей начальной части корня."""
    if not stem1 or not stem2:
        return False
    if stem1 == stem2:
        return True
    
    min_len = min(len(stem1), len(stem2))
    if min_len >= 5:
        prefix_len = min_len - 1
        return stem1[:prefix_len] == stem2[:prefix_len]
    return False


def load_caches() -> None:
    global _friends_cache, _relatives_cache
    if FRIENDS_CACHE_FILE.exists():
        try:
            raw = json.loads(FRIENDS_CACHE_FILE.read_text(encoding="utf-8"))
            _friends_cache = {int(k): v for k, v in raw.items()}
            print(f"Кэш друзей загружен: {len(_friends_cache)} пользователей")
        except Exception as e:
            print(f"  [кэш друзей] не удалось загрузить: {e}")

    if RELATIVES_CACHE_FILE.exists():
        try:
            raw = json.loads(RELATIVES_CACHE_FILE.read_text(encoding="utf-8"))
            _relatives_cache = {int(k): v for k, v in raw.items()}
            print(f"Кэш родственников загружен: {len(_relatives_cache)} пользователей")
        except Exception as e:
            print(f"  [кэш родственников] не удалось загрузить: {e}")


def save_caches() -> None:
    try:
        FRIENDS_CACHE_FILE.write_text(
            json.dumps({str(k): v for k, v in _friends_cache.items()}, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception as e:
        log(f"  [кэш друзей] не удалось сохранить: {e}")

    try:
        RELATIVES_CACHE_FILE.write_text(
            json.dumps({str(k): v for k, v in _relatives_cache.items()}, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception as e:
        log(f"  [кэш родственников] не удалось сохранить: {e}")


def handle_exit(signum=None, frame=None) -> None:
    print("\n\n[Прервано пользователем] Сохраняю кэш перед выходом...")
    save_caches()
    sys.exit(0)


signal.signal(signal.SIGINT, handle_exit)


def load_token(token_arg: str | None) -> str:
    if token_arg:
        TOKEN_FILE.write_text(token_arg.strip(), encoding="utf-8")
        return token_arg.strip()
    if TOKEN_FILE.exists():
        return TOKEN_FILE.read_text(encoding="utf-8").strip()
    print("Токен не найден. Укажи его через --token:")
    print("  python vk_chain.py user1 --token vk1.a.XXX")
    sys.exit(1)


def log(msg: str) -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def api(token: str, method: str, **params) -> dict | None:
    global _last

    while True:
        with _rate_lock:
            now = time.monotonic()
            wait = DELAY - (now - _last)
            if wait > 0:
                time.sleep(wait)
            _last = time.monotonic()

        params_copy = dict(params)
        params_copy.update({"access_token": token, "v": V})
        try:
            r = _session.post(f"{API}/{method}", data=params_copy, timeout=12)
            data = r.json()
        except Exception as e:
            log(f"  [net] {e}")
            return None

        if "error" in data:
            code = data["error"]["error_code"]
            msg = data["error"].get("error_msg", "")

            if code in (15, 30, 18):
                return None
            elif code == 6:
                time.sleep(0.5)
                continue
            elif code == 14:
                log("  [api] Требуется капча! Ждём 10 секунд...")
                time.sleep(10.0)
                continue

            log(f"  [api {code}] {msg}")
            return None

        return data.get("response")


def resolve_user(token: str, identifier: str) -> tuple[int, str, str, str] | None:
    resp = api(token, "users.get", user_ids=identifier)
    if not resp or not isinstance(resp, list):
        return None
    u = resp[0]
    if u.get("deactivated"):
        return None
    fn = u.get("first_name", "")
    ln = u.get("last_name", "")
    return u["id"], f"{fn} {ln}".strip(), fn, ln


def fetch_user_profiles(token: str, ids: list[int]) -> dict[int, dict]:
    profiles = {}
    for i in range(0, len(ids), 1000):
        chunk = ids[i:i + 1000]
        resp = api(token, "users.get", user_ids=",".join(map(str, chunk)), fields="photo_100,domain,last_name,first_name")
        if resp and isinstance(resp, list):
            for u in resp:
                fn = u.get("first_name", "")
                ln = u.get("last_name", "")
                profiles[u["id"]] = {
                    "id": u["id"],
                    "name": f"{fn} {ln}".strip(),
                    "first_name": fn,
                    "last_name": ln,
                    "photo": u.get("photo_100", "https://vk.com/images/camera_100.png"),
                    "domain": u.get("domain", f"id{u['id']}")
                }
    return profiles


def _fetch_execute_chunk(token: str, chunk: list[int]) -> dict[int, list[int]]:
    res: dict[int, list[int]] = {}
    code_parts = [
        f'"{uid}": API.friends.get({{"user_id": {uid}, "count": 5000}}).items'
        for uid in chunk
    ]
    script = "return {" + ",".join(code_parts) + "};"

    resp = api(token, "execute", code=script)
    if resp and isinstance(resp, dict):
        for uid_str, friends in resp.items():
            uid = int(uid_str)
            res[uid] = friends if isinstance(friends, list) else []
    else:
        for uid in chunk:
            res[uid] = []
    return res


def get_friends_batch(token: str, user_ids: list[int]) -> dict[int, list[int]]:
    result: dict[int, list[int]] = {}
    to_fetch = [uid for uid in user_ids if uid not in _friends_cache]

    for uid in user_ids:
        if uid in _friends_cache:
            result[uid] = _friends_cache[uid]

    if not to_fetch:
        return result

    chunk_size = 25
    chunks = [to_fetch[i:i + chunk_size] for i in range(0, len(to_fetch), chunk_size)]

    with ThreadPoolExecutor(max_workers=3) as executor:
        future_to_chunk = {
            executor.submit(_fetch_execute_chunk, token, chunk): chunk 
            for chunk in chunks
        }
        for future in as_completed(future_to_chunk):
            chunk_res = future.result()
            for uid, friends in chunk_res.items():
                _friends_cache[uid] = friends
                result[uid] = friends

    return result


def find_surnames_and_relatives_deep(token: str, identifier: str, custom_surname: str | None = None, deep: bool = False) -> None:
    """Ищет родственников/однофамильцев. С флагом deep идет дальше вглубь по графу, пока не остановить (Ctrl+C)."""
    user_info = resolve_user(token, identifier)
    if not user_info:
        print(f"Пользователь '{identifier}' не найден или заблокирован.")
        return

    start_uid, start_full_name, start_fn, start_ln = user_info

    if custom_surname:
        words = custom_surname.split()
    else:
        words = [w for w in (start_fn.split() + start_ln.split()) if len(w) > 2]

    stems_target = {get_surname_stem(w) for w in words if len(w) > 2}

    print(f"\n==================================================")
    print(f"Старт поиска для: {start_full_name} (id{start_uid})")
    print(f"Искомая фамилия: '{custom_surname or start_ln}'")
    print(f"Анализируемые корни: {', '.join(stems_target)}")
    if deep:
        print("РЕЖИМ ГЛУБОКОГО ПОИСКА АКТИВИРОВАН. Для остановки нажмите Ctrl+C")
    print(f"==================================================\n")

    queue = deque([(start_uid, 0)])
    visited = {start_uid}

    while queue:
        current_uid, depth = queue.popleft()

        prof_resp = fetch_user_profiles(token, [current_uid])
        current_prof = prof_resp.get(current_uid, {"name": f"id{current_uid}", "domain": f"id{current_uid}"})

        print(f"\n--- [Уровень {depth}] Сканируем: {current_prof['name']} (id{current_uid}) ---")

        # 1. Проверка прямых родственников VK
        resp = api(token, "users.get", user_ids=current_uid, fields="relatives")
        if resp and isinstance(resp, list):
            relatives = resp[0].get("relatives", [])
            linked = [r for r in relatives if r.get("id") and r.get("id") > 0]
            if linked:
                rel_ids = [r["id"] for r in linked]
                profiles = fetch_user_profiles(token, rel_ids)
                print(f" Указано в блоке 'Родственники' VK ({len(linked)}):")
                for r in linked:
                    rid = r["id"]
                    rel_type = RELATIVE_TYPES.get(r.get("type"), "Родственник")
                    prof = profiles.get(rid, {"name": f"id{rid}", "domain": f"id{rid}"})
                    print(f"  • [{rel_type}] {prof['name']} (id{rid}) — https://vk.com/{prof['domain']}")
                    
                    if deep and rid not in visited:
                        visited.add(rid)
                        queue.append((rid, depth + 1))

        # 2. Поиск совпадений среди друзей
        print(" Получение списка друзей...")
        friends_dict = get_friends_batch(token, [current_uid])
        friend_ids = friends_dict.get(current_uid, [])

        if not friend_ids:
            print(" У пользователя нет доступных друзей или профиль закрыт.")
            if not deep:
                break
            continue

        print(f" Проверка {len(friend_ids)} друзей на совпадение...")
        profiles = fetch_user_profiles(token, friend_ids)

        matches = []
        for fid, prof in profiles.items():
            friend_words = prof.get("first_name", "").split() + prof.get("last_name", "").split()
            friend_stems = {get_surname_stem(w) for w in friend_words if len(w) > 2}

            is_matched = False
            for target_stem in stems_target:
                for friend_stem in friend_stems:
                    if is_stem_match(target_stem, friend_stem):
                        is_matched = True
                        break
                if is_matched:
                    break

            if is_matched:
                matches.append(prof)
                if deep and fid not in visited:
                    visited.add(fid)
                    queue.append((fid, depth + 1))

        if matches:
            print(f" Найдено совпадений среди друзей ({len(matches)}):")
            for m in matches:
                print(f"  • {m['name']} (id{m['id']}) — https://vk.com/{m['domain']}")
        else:
            print(" Совпадений среди прямых друзей не обнаружено.")

        if not deep:
            break

    print("\n[Поиск завершён или очередь пуста].")


def build_path(meeting: int, current: int, parent: dict, other_parent: dict, from_a: bool) -> list[int]:
    left: list[int] = []
    cur: int | None = current
    while cur is not None:
        left.append(cur)
        cur = parent.get(cur)

    right: list[int] = []
    cur = meeting
    while cur is not None:
        right.append(cur)
        cur = other_parent.get(cur)

    if from_a:
        left.reverse()
        return left + right
    else:
        right.reverse()
        return right + left


def expand_layer(
    token: str, frontier: list[int],
    parent: dict, other_parent: dict,
    label: str, depth: int,
    banned: set[int],
    include_relatives: bool = False
) -> tuple[list[int], list[int] | None]:
    next_frontier: list[int] = []
    accessible = 0
    from_a = (label == "A→")
    total = len(frontier)

    chunk_size = 25
    for i in range(0, total, chunk_size):
        chunk = frontier[i:i + chunk_size]
        progress = min(i + chunk_size, total)
        print(f"  [{label}] depth={depth}  {progress}/{total}   ", end="\r")

        friends_map = get_friends_batch(token, chunk)
        relatives_map = {}

        for uid in chunk:
            friends = set(friends_map.get(uid, []))
            relatives = set(relatives_map.get(uid, []))
            connections = friends | relatives

            if connections:
                accessible += 1

            for fid in connections:
                if fid in banned:
                    continue
                if fid in other_parent:
                    print()
                    return next_frontier, build_path(fid, uid, parent, other_parent, from_a)
                if fid not in parent and fid not in other_parent:
                    parent[fid] = uid
                    next_frontier.append(fid)

    log(f"[{label}] depth={depth}: {len(next_frontier)} новых, {accessible}/{total} открытых")
    return next_frontier, None


def find_chain(
    token: str, start_id: int, end_id: int, 
    banned: set[int] | None = None, 
    include_relatives: bool = False
) -> list[int] | None:
    if start_id == end_id:
        return [start_id]

    banned = banned or set()
    parent_a: dict[int, int | None] = {start_id: None}
    parent_b: dict[int, int | None] = {end_id: None}
    frontier_a, frontier_b = [start_id], [end_id]
    depth_a = depth_b = 0

    while frontier_a or frontier_b:
        if frontier_a and (not frontier_b or len(frontier_a) <= len(frontier_b)):
            depth_a += 1
            frontier_a, path = expand_layer(
                token, frontier_a, parent_a, parent_b, "A→", depth_a, banned, include_relatives
            )
            if path:
                return path
        elif frontier_b:
            depth_b += 1
            frontier_b, path = expand_layer(
                token, frontier_b, parent_b, parent_a, "←B", depth_b, banned, include_relatives
            )
            if path:
                return path

    return None


def export_html(start_id: int, end_id: int, found_chains: list[list[int]], profiles: dict[int, dict]) -> None:
    PHOTOS_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    start_name = profiles.get(start_id, {}).get("name", f"id{start_id}")
    end_name = profiles.get(end_id, {}).get("name", f"id{end_id}")

    for idx, chain in enumerate(found_chains, 1):
        export_filename = PHOTOS_DIR / f"vk_chain_id{start_id}_to_id{end_id}_path{idx}_{timestamp}.html"

        html_content = f"""<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <title>Цепочка {idx}: id{start_id} → id{end_id}</title>
    <style>
        body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: #0e1621; color: #ffffff; padding: 20px; }}
        h1 {{ text-align: center; color: #5288c1; margin-bottom: 5px; }}
        .subtitle {{ text-align: center; color: #7f91a4; font-size: 14px; margin-bottom: 30px; }}
        .chain-container {{ display: flex; flex-direction: column; gap: 30px; align-items: center; }}
        .chain-row {{ display: flex; flex-wrap: wrap; align-items: center; justify-content: center; background: #17212b; padding: 20px; border-radius: 12px; box-shadow: 0 4px 15px rgba(0,0,0,0.5); }}
        .user-card {{ display: flex; flex-direction: column; align-items: center; text-decoration: none; color: white; width: 120px; transition: transform 0.2s; }}
        .user-card:hover {{ transform: scale(1.08); }}
        .avatar {{ width: 80px; height: 80px; border-radius: 50%; border: 3px solid #5288c1; object-fit: cover; margin-bottom: 8px; }}
        .name {{ font-size: 13px; text-align: center; font-weight: bold; line-height: 1.2; word-break: break-word; }}
        .arrow {{ font-size: 24px; color: #5288c1; margin: 0 15px; font-weight: bold; }}
    </style>
</head>
<body>
    <h1>Маршрут #{idx}</h1>
    <div class="subtitle">{start_name} (id{start_id}) ➔ {end_name} (id{end_id})</div>
    <div class="chain-container">
        <div class="chain-row">
"""
        for i, uid in enumerate(chain):
            p = profiles.get(uid, {"name": f"id{uid}", "photo": "https://vk.com/images/camera_100.png", "domain": f"id{uid}"})
            html_content += f'''            <a class="user-card" href="https://vk.com/{p['domain']}" target="_blank">
                <img class="avatar" src="{p['photo']}" alt="{p['name']}">
                <div class="name">{p['name']}</div>
            </a>\n'''
            if i < len(chain) - 1:
                html_content += '            <div class="arrow">➔</div>\n'

        html_content += """        </div>
    </div>
</body>
</html>"""

        export_filename.write_text(html_content, encoding="utf-8")
        print(f"[Экспорт] Сохранён HTML-файл пути #{idx}: {export_filename.resolve()}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Поиск цепочек рукопожатий и однофамильцев/родственников в VK.",
        add_help=True
    )
    parser.add_argument("user1", help="ID или short_name первого пользователя")
    parser.add_argument("user2", nargs="?", default=None, help="ID второго пользователя")
    parser.add_argument("--get-relatives", action="store_true", help="Поиск родственников и однофамильцев первого пользователя")
    parser.add_argument("-s", "--surname", default=None, help="Вручную указать фамилию для поиска среди друзей")
    parser.add_argument("-d", "--deep", action="store_true", help="Идти вглубь по графу родственников/однофамильцев, пока не остановите (Ctrl+C)")
    parser.add_argument("--via", default=None, metavar="USER", help="Промежуточный пользователь в цепочке")
    parser.add_argument("--token", default=None, help="VK access token")
    parser.add_argument("--paths", type=int, default=1, metavar="N", help="Найти N уникальных маршрутов")
    parser.add_argument("--export", action="store_true", help="Экспортировать маршруты в HTML-файлы")
    parser.add_argument("--include-relatives", action="store_true", help="Искать цепочки не только по друзьям, но и по родственникам")
    parser.add_argument("--clear-cache", action="store_true", help="Очистить локальный кэш")
    
    args = parser.parse_args()

    token = load_token(args.token)

    if args.clear_cache:
        if FRIENDS_CACHE_FILE.exists():
            FRIENDS_CACHE_FILE.unlink()
        if RELATIVES_CACHE_FILE.exists():
            RELATIVES_CACHE_FILE.unlink()
        print("Кэш очищен.")

    load_caches()

    # Режим поиска родственников и однофамильцев (одноуровневый или глубокий)
    if args.get_relatives or args.surname or args.deep:
        find_surnames_and_relatives_deep(token, args.user1, custom_surname=args.surname, deep=args.deep)
        save_caches()
        sys.exit(0)

    # Поиск цепочек
    if not args.user2:
        print("Ошибка: Для поиска цепочки необходимо указать второго пользователя (user2) или использовать --get-relatives / -s / -d.")
        sys.exit(1)

    with LOG_FILE.open("w", encoding="utf-8") as f:
        f.write(f"=== VK Chain Log {datetime.now()} ===\n")

    print("Резолвлю пользователей...")
    a = resolve_user(token, args.user1)
    b = resolve_user(token, args.user2)
    c = resolve_user(token, args.via) if args.via else None

    if not a or not b or (args.via and not c):
        print("Не удалось найти одного из пользователей.")
        sys.exit(1)

    print(f"От:   {a[1]} (id{a[0]})")
    if c:
        print(f"Через: {c[1]} (id{c[0]})")
    print(f"До:   {b[1]} (id{b[0]})")

    found: list[list[int]] = []
    banned: set[int] = set()

    for attempt in range(args.paths):
        label = f"маршрут {attempt + 1}/{args.paths}" if args.paths > 1 else "цепочку"
        print(f"\nИщу {label}...\n")

        if c:
            chain1 = find_chain(token, a[0], c[0], banned, args.include_relatives)
            if chain1:
                chain2 = find_chain(token, c[0], b[0], banned, args.include_relatives)
                full_chain = chain1 + chain2[1:] if chain2 else None
            else:
                full_chain = None
        else:
            full_chain = find_chain(token, a[0], b[0], banned, args.include_relatives)

        if full_chain is None:
            break

        found.append(full_chain)
        save_caches()

        all_ids = list({uid for ch in found for uid in ch})
        profiles = fetch_user_profiles(token, all_ids)

        print(f"\nМаршрут {len(found)}: {len(full_chain) - 1} рукопожатий")
        print("  " + " → ".join([f"{profiles.get(uid, {}).get('name', f'id{uid}')} (id{uid})" for uid in full_chain]))

        middle_uids = set(full_chain[1:-1])
        middle_uids.discard(a[0])
        middle_uids.discard(b[0])
        if c:
            middle_uids.discard(c[0])
        banned.update(middle_uids)

    if found and args.export:
        all_ids = list({uid for ch in found for uid in ch})
        profiles = fetch_user_profiles(token, all_ids)
        export_html(a[0], b[0], found, profiles)

    save_caches()


if __name__ == "__main__":
    main()