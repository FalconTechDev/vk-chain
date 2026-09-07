#!/usr/bin/env python3
"""
VK Family & Relatives Finder
Автоматический поиск вероятных родственников по ID, короткому имени или Имени Фамилии.
"""

import json
import sys
import time
import signal
import argparse
from datetime import datetime
from pathlib import Path
import requests

API = "https://api.vk.com/method"
V = "5.131"
LOG_FILE = Path("vk_relatives_log.txt")
TOKEN_FILE = Path.home() / ".vk_chain_token"
PHOTOS_DIR = Path("relatives_reports")

_session = requests.Session()
_last_req = 0.0
DELAY = 0.35


def load_token(token_arg: str | None) -> str:
    if token_arg:
        TOKEN_FILE.write_text(token_arg.strip(), encoding="utf-8")
        return token_arg.strip()
    if TOKEN_FILE.exists():
        return TOKEN_FILE.read_text(encoding="utf-8").strip()
    print("Токен не найден! Укажите его один раз через --token:")
    print("  python vk_relatives.py 'Иван Иванов' --token vk1.a.XXX")
    sys.exit(1)


def api(token: str, method: str, **params) -> dict | None:
    global _last_req
    now = time.monotonic()
    wait = DELAY - (now - _last_req)
    if wait > 0:
        time.sleep(wait)
    _last_req = time.monotonic()

    params.update({"access_token": token, "v": V})
    try:
        r = _session.post(f"{API}/{method}", data=params, timeout=12)
        data = r.json()
    except Exception as e:
        print(f" [ошибка сети] {e}")
        return None

    if "error" in data:
        err = data["error"]
        print(f" [VK API Error {err.get('error_code')}] {err.get('error_msg')}")
        return None

    return data.get("response")


def resolve_target_user(token: str, query: str) -> dict | None:
    """Определяет пользователя по ID, screen_name или поисковому запросу 'Имя Фамилия'."""
    query = query.strip()
    
    # 1. Пробуем отрезолвить как ID / screen_name
    fields = "photo_100,bdate,relatives,relation,relation_partner,last_name,first_name,domain"
    resp = api(token, "users.get", user_ids=query, fields=fields)
    if resp and isinstance(resp, list) and len(resp) > 0 and "id" in resp[0]:
        return resp[0]

    # 2. Если не вышло — ищем через VK Search по имени/фамилии
    print(f"Поиск пользователя по запросу '{query}'...")
    search_resp = api(token, "users.search", q=query, count=5, fields=fields)
    if search_resp and "items" in search_resp and search_resp["items"]:
        items = search_resp["items"]
        print("\nНайдены следующие пользователи:")
        for idx, item in enumerate(items, 1):
            city = item.get("city", {}).get("title", "Город не указан")
            bdate = item.get("bdate", "ДР скрыт")
            print(f" [{idx}] {item['first_name']} {item['last_name']} (id{item['id']}) — {city}, {bdate}")
        
        choice = input("\nВыберите номер пользователя (по умолчанию 1): ").strip()
        selected_idx = int(choice) - 1 if choice.isdigit() and 0 < int(choice) <= len(items) else 0
        return items[selected_idx]

    return None


def parse_birth_year(bdate_str: str | None) -> int | None:
    if not bdate_str:
        return None
    parts = bdate_str.split(".")
    if len(parts) == 3:
        return int(parts[2])
    return None


def find_relatives(token: str, target: dict) -> dict:
    target_id = target["id"]
    target_lname = target["last_name"].lower()
    target_byear = parse_birth_year(target.get("bdate"))

    relatives_data = {
        "explicit": [],   # Указаны прямо в профиле (VK relatives)
        "partner": None,   # Супруг / Супруга
        "same_surname": [], # Друзья с такой же фамилией
        "possible_parents": [],
        "possible_kids": []
    }

    # 1. Поиск прямо указанного партнера
    if "relation_partner" in target:
        relatives_data["partner"] = target["relation_partner"]

    # 2. Явно указанные родственники
    explicit_ids = []
    if "relatives" in target:
        for rel in target["relatives"]:
            if "id" in rel and rel["id"] > 0:
                explicit_ids.append((rel["id"], rel.get("type", "relative")))

    if explicit_ids:
        u_ids = ",".join(str(x[0]) for x in explicit_ids)
        profiles = api(token, "users.get", user_ids=u_ids, fields="photo_100,bdate,domain")
        if profiles:
            prof_map = {p["id"]: p for p in profiles}
            for uid, rel_type in explicit_ids:
                if uid in prof_map:
                    p = prof_map[uid]
                    p["rel_type"] = rel_type
                    relatives_data["explicit"].append(p)

    # 3. Сканируем друзей на совпадение фамилии и возрастные группы
    print("Получаем список друзей для детального анализа...")
    friends_resp = api(token, "friends.get", user_id=target_id, fields="photo_100,bdate,domain,nickname")
    
    if friends_resp and "items" in friends_resp:
        friends = friends_resp["items"]
        print(f"Проанализировано друзей: {len(friends)}")

        for friend in friends:
            f_id = friend["id"]
            if f_id in [e["id"] for e in relatives_data["explicit"]]:
                continue # Уже учтён

            f_lname = friend.get("last_name", "").lower()
            f_byear = parse_birth_year(friend.get("bdate"))

            # Совпадение по фамилии
            if f_lname == target_lname:
                relatives_data["same_surname"].append(friend)

            # Гипотеза о родителях/детях по годам рождения
            if target_byear and f_byear:
                diff = target_byear - f_byear
                if 17 <= diff <= 45: # Друг старше на 17-45 лет -> Вероятный родитель
                    if f_lname == target_lname or friend.get("first_name").endswith(("вна", "вич")):
                        relatives_data["possible_parents"].append(friend)
                elif 17 <= -diff <= 45: # Друг младше на 17-45 лет -> Вероятный ребёнок
                    if f_lname == target_lname:
                        relatives_data["possible_kids"].append(friend)

    return relatives_data


def export_html_report(target: dict, relatives: dict) -> Path:
    PHOTOS_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filepath = PHOTOS_DIR / f"family_{target['id']}_{timestamp}.html"

    target_name = f"{target['first_name']} {target['last_name']}"

    def make_card(user_info, note=""):
        photo = user_info.get("photo_100", "https://vk.com/images/camera_100.png")
        name = f"{user_info.get('first_name', '')} {user_info.get('last_name', '')}"
        domain = user_info.get("domain", f"id{user_info['id']}")
        bdate = user_info.get("bdate", "")
        return f"""
        <div class="user-card">
            <a href="https://vk.com/{domain}" target="_blank">
                <img class="avatar" src="{photo}" alt="{name}">
            </a>
            <div class="name"><a href="https://vk.com/{domain}" target="_blank">{name}</a></div>
            {f'<div class="meta">{bdate}</div>' if bdate else ''}
            {f'<div class="badge">{note}</div>' if note else ''}
        </div>
        """

    html = f"""<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <title>Семейное дерево: {target_name}</title>
    <style>
        body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: #0e1621; color: #ffffff; padding: 30px; }}
        h1, h2 {{ text-align: center; color: #5288c1; }}
        .section {{ background: #17212b; padding: 20px; border-radius: 12px; margin-bottom: 25px; box-shadow: 0 4px 10px rgba(0,0,0,0.3); }}
        .grid {{ display: flex; flex-wrap: wrap; gap: 20px; justify-content: center; margin-top: 15px; }}
        .user-card {{ background: #242f3d; padding: 15px; border-radius: 10px; width: 140px; text-align: center; display: flex; flex-direction: column; align-items: center; }}
        .avatar {{ width: 80px; height: 80px; border-radius: 50%; border: 2px solid #5288c1; object-fit: cover; margin-bottom: 8px; }}
        .name a {{ color: #ffffff; text-decoration: none; font-weight: bold; font-size: 13px; }}
        .name a:hover {{ text-decoration: underline; }}
        .meta {{ font-size: 11px; color: #7f91a4; margin-top: 4px; }}
        .badge {{ margin-top: 6px; background: #2b5278; font-size: 10px; padding: 3px 6px; border-radius: 4px; color: #e3ecf4; }}
        .target-card .avatar {{ border-color: #e54242; width: 100px; height: 100px; }}
    </style>
</head>
<body>
    <h1>Анализ родственных связей</h1>
    
    <div class="section" style="text-align: center;">
        <h2>Целевой профиль</h2>
        <div class="grid">
            <div class="user-card target-card" style="width: 180px;">
                <img class="avatar" src="{target.get('photo_100')}" alt="{target_name}">
                <div class="name"><a href="https://vk.com/id{target['id']}" target="_blank">{target_name}</a></div>
                <div class="meta">ID: {target['id']}</div>
                <div class="meta">{target.get('bdate', '')}</div>
            </div>
        </div>
    </div>
"""

    # Подтверждённые родственники
    if relatives["explicit"] or relatives["partner"]:
        html += '<div class="section"><h2>Указаны в профиле (Подтверждённые)</h2><div class="grid">'
        if relatives["partner"]:
            html += make_card(relatives["partner"], "Супруг/Партнёр")
        for r in relatives["explicit"]:
            html += make_card(r, f"Тип: {r.get('rel_type', 'родственник')}")
        html += '</div></div>'

    # Однофамильцы из друзей
    if relatives["same_surname"]:
        html += '<div class="section"><h2>Друзья с такой же фамилией</h2><div class="grid">'
        for r in relatives["same_surname"]:
            html += make_card(r, "Однофамилец")
        html += '</div></div>'

    # Вероятные родители / дети
    if relatives["possible_parents"]:
        html += '<div class="section"><h2>Вероятные родители / Старшее поколение</h2><div class="grid">'
        for r in relatives["possible_parents"]:
            html += make_card(r, "Возм. родитель")
        html += '</div></div>'

    if relatives["possible_kids"]:
        html += '<div class="section"><h2>Вероятные дети / Младшее поколение</h2><div class="grid">'
        for r in relatives["possible_kids"]:
            html += make_card(r, "Возм. ребёнок")
        html += '</div></div>'

    html += "</body></html>"
    filepath.write_text(html, encoding="utf-8")
    return filepath


def main():
    parser = argparse.ArgumentParser(description="Поиск родственников и однофамильцев пользователя VK.")
    parser.add_argument("query", help="Имя и Фамилия ('Иван Иванов'), короткий адрес ('durov') или ID ('123456')")
    parser.add_argument("--token", default=None, help="VK access token")
    args = parser.parse_args()

    token = load_token(args.token)

    print(f"Ищем профиль по запросу: '{args.query}'...")
    target = resolve_target_user(token, args.query)

    if not target:
        print("Пользователь не найден.")
        sys.exit(1)

    print(f"\nВыбран профиль: {target['first_name']} {target['last_name']} (ID: {target['id']})")
    print("Собираем данные о родственниках и друзьях...")

    relatives = find_relatives(token, target)

    print("\n--- Результаты анализа ---")
    print(f" Напрямую указано родственников: {len(relatives['explicit'])}")
    print(f" Найдено друзей-однофамильцев: {len(relatives['same_surname'])}")
    print(f" Вероятных родителей/старших: {len(relatives['possible_parents'])}")
    print(f" Вероятных детей/младших: {len(relatives['possible_kids'])}")

    report_path = export_html_report(target, relatives)
    print(f"\n[Экспорт] Полный отчёт сохранён в HTML:")
    print(f" -> {report_path.resolve()}")


if __name__ == "__main__":
    main()