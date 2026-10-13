"""テストの最後に答えてもらう評価フォーム (サイトの /feedback)。

サイトの見た目や使いやすさの 6 段階評価に加えて、住んでいる場所・回線の速さ・Wi-Fi・遊び方・
払ってもいい金額・我慢できる FPS など、あとで料金や貸し手の条件を決めるのに使うデータを集める。
Discord ボットから渡すリンクには ?t=<テストの回> が付き、ボットの記録とつなげて集計できる。
"""

from __future__ import annotations

import csv
import io
import json
import sqlite3
import statistics
from pathlib import Path

SCALE = [(1, "とても悪い"), (2, "悪い"), (3, "やや悪い"), (4, "やや良い"), (5, "良い"), (6, "とても良い")]

# 6 段階で評価する項目。「わからない・使っていない」は 0
RATINGS: list[tuple[str, str]] = [
    ("design", "サイトの見た目（デザイン）"),
    ("name", "「Geaspec（ギアスペック）」という名前のセンス"),
    ("readability", "文字やボタンの見やすさ"),
    ("booking_flow", "予約までの流れのわかりやすさ"),
    ("download_flow", "アプリのダウンロード・インストールまでの流れ（使った人だけ）"),
    ("mobile", "スマホでの使いやすさ"),
    ("trust", "安心して使えそうか"),
    ("comfort", "遊んでいるときの快適さ（画質・遅れ・カクつき）"),
    ("overall", "全体の満足度"),
]

PREFECTURES = [
    "北海道", "青森県", "岩手県", "宮城県", "秋田県", "山形県", "福島県", "茨城県", "栃木県", "群馬県", "埼玉県",
    "千葉県", "東京都", "神奈川県", "新潟県", "富山県", "石川県", "福井県", "山梨県", "長野県", "岐阜県", "静岡県",
    "愛知県", "三重県", "滋賀県", "京都府", "大阪府", "兵庫県", "奈良県", "和歌山県", "鳥取県", "島根県", "岡山県",
    "広島県", "山口県", "徳島県", "香川県", "愛媛県", "高知県", "福岡県", "佐賀県", "長崎県", "熊本県", "大分県",
    "宮崎県", "鹿児島県", "沖縄県", "海外",
]


def _opts(*labels: str) -> list[tuple[str, str]]:
    return [(str(i), label) for i, label in enumerate(labels, start=1)]


# えらぶ項目。section ごとに画面で見出しを分ける
CHOICES: list[dict] = [
    {"section": "env", "key": "prefecture", "label": "住んでいる都道府県", "options": [(p, p) for p in PREFECTURES],
     "select": True},
    {"section": "env", "key": "line", "label": "家のインターネット回線",
     "options": _opts("光回線", "ホームルーター（コンセントに挿すだけの Wi-Fi）", "ケーブルテレビ", "スマホの回線・テザリング", "わからない")},
    {"section": "env", "key": "wifi_band", "label": "Quest をつないだ Wi-Fi の種類",
     "options": _opts("6GHz（Wi-Fi 6E）", "5GHz", "2.4GHz", "わからない")},
    {"section": "env", "key": "router_distance", "label": "Quest とルーターの距離",
     "options": _opts("同じ部屋", "となりの部屋", "離れた部屋・別の階", "わからない")},
    {"section": "env", "key": "quest", "label": "使った Quest", "options": _opts("Quest 2", "Quest 3", "Quest 3S", "Quest Pro", "その他")},
    {"section": "env", "key": "own_pc", "label": "自分のパソコン",
     "options": _opts("持っていない", "VR はできないパソコンならある", "VR ができるパソコンがある")},
    {"section": "play", "key": "vrc_days", "label": "VRChat で遊ぶ日数",
     "options": _opts("ほぼ毎日", "週に3〜5日", "週に1〜2日", "それより少ない")},
    {"section": "play", "key": "vrc_hours", "label": "1回に遊ぶ時間", "options": _opts("1時間より短い", "1〜2時間", "2〜4時間", "4時間以上")},
    {"section": "play", "key": "play_times", "label": "よく遊ぶ時間帯（いくつでも）", "multi": True,
     "options": _opts("平日の夕方（〜19時）", "平日の夜（19〜24時）", "平日の深夜（24時〜）", "休日の昼", "休日の夜", "休日の深夜")},
    {"section": "play", "key": "session_length", "label": "PC を借りるなら、1回にどのくらい借りたいか",
     "options": _opts("30分", "1時間", "2時間", "3時間以上")},
    {"section": "money", "key": "hourly_price", "label": "1時間いくらまでなら払うか",
     "options": _opts("〜200円", "〜400円", "〜600円", "〜800円", "〜1,000円", "それ以上")},
    {"section": "money", "key": "monthly_budget", "label": "1か月にいくらまでなら使えるか",
     "options": _opts("〜1,000円", "〜3,000円", "〜5,000円", "〜10,000円", "それ以上")},
    {"section": "money", "key": "pay_style", "label": "いちばんうれしい払い方",
     "options": _opts("使うたびに払う", "前払いのチケット（まとめ買いで少し安い）", "月額で決まった時間まで", "わからない")},
    {"section": "money", "key": "pc_budget", "label": "VR 用のパソコンを買うなら、いくらまで出せるか",
     "options": _opts("買うつもりはない", "〜10万円", "〜15万円", "〜20万円", "20万円より上")},
    {"section": "comfort", "key": "min_fps", "label": "この FPS を下回ったら嫌だ、という線",
     "options": _opts("30", "45", "60", "72", "90", "わからない")},
    {"section": "comfort", "key": "max_delay", "label": "操作の遅れはどのくらい気になったか",
     "options": _opts("まったく気にならない", "少し気になったが平気", "けっこう気になった", "がまんできない")},
]

# 数字で答える項目 (どれも空でよい)
NUMBERS: list[dict] = [
    {"key": "down_mbps", "label": "下り（ダウンロード）", "unit": "Mbps", "max": 20000},
    {"key": "up_mbps", "label": "上り（アップロード）", "unit": "Mbps", "max": 20000},
    {"key": "speed_ping", "label": "Ping（応答時間）", "unit": "ms", "max": 5000},
]

TEXTS: list[tuple[str, str]] = [
    ("good", "よかったところ"),
    ("bad", "直してほしいところ・不安なこと"),
    ("idea", "名前・デザイン・料金について、ひとこと"),
]

SECTIONS = {
    "env": "あなたの環境",
    "play": "ふだんの遊び方",
    "money": "お金のこと",
    "comfort": "快適さの線引き",
}


class FeedbackError(ValueError):
    pass


def parse(form: dict[str, list[str]]) -> dict:
    """フォームの値を確かめて、保存する形にする。form は {名前: [値, ...]}。"""
    answers: dict = {}
    for key, _ in RATINGS:
        value = (form.get(key) or [""])[0]
        if value not in {str(n) for n in range(0, 7)}:
            raise FeedbackError("6段階の評価に、答えていない項目があります")
        answers[key] = int(value)
    for q in CHOICES:
        allowed = {v for v, _ in q["options"]}
        values = [v for v in form.get(q["key"], []) if v]
        if not values or any(v not in allowed for v in values) or (len(values) > 1 and not q.get("multi")):
            raise FeedbackError(f"「{q['label']}」をえらんでください")
        answers[q["key"]] = values if q.get("multi") else values[0]
    for q in NUMBERS:
        raw = (form.get(q["key"]) or [""])[0].strip().replace(",", "")
        if not raw:
            continue
        try:
            number = float(raw)
        except ValueError as exc:
            raise FeedbackError(f"「{q['label']}」は数字で入れてください") from exc
        if not 0 <= number <= q["max"]:
            raise FeedbackError(f"「{q['label']}」の数字を確かめてください")
        answers[q["key"]] = round(number, 1)
    for key, _ in TEXTS:
        text = (form.get(key) or [""])[0].strip()[:1000]
        if text:
            answers[key] = text
    return answers


def label_of(key: str, value) -> str:
    q = next((q for q in CHOICES if q["key"] == key), None)
    if q is None:
        return str(value)
    names = dict(q["options"])
    if isinstance(value, list):
        return "・".join(names.get(v, v) for v in value)
    return names.get(value, str(value))


def columns() -> list[tuple[str, str]]:
    """CSV の列 (キー, 見出し)。"""
    return ([(k, label) for k, label in RATINGS] + [(q["key"], q["label"]) for q in CHOICES]
            + [(q["key"], f"{q['label']}({q['unit']})") for q in NUMBERS] + list(TEXTS))


def cell(answers: dict, key: str) -> str:
    value = answers.get(key)
    if value is None:
        return ""
    if key in dict(RATINGS):
        return "" if value == 0 else str(value)
    if any(q["key"] == key for q in CHOICES):
        return label_of(key, value)
    return str(value)


def summarize(rows: list[dict]) -> dict:
    """rows は {"answers": {...}} のリスト。6 段階の平均 (「わからない」を除く)・えらんだ数・数字の真ん中の値。"""
    answers = [r["answers"] for r in rows]
    ratings = {}
    for key, label in RATINGS:
        values = [a[key] for a in answers if a.get(key)]
        ratings[key] = {"label": label, "avg": round(statistics.mean(values), 2) if values else None, "n": len(values)}
    choices = {}
    for q in CHOICES:
        counts = {v: 0 for v, _ in q["options"]}
        for a in answers:
            picked = a.get(q["key"])
            for v in (picked if isinstance(picked, list) else [picked] if picked else []):
                if v in counts:
                    counts[v] += 1
        choices[q["key"]] = {"label": q["label"], "counts": [(label, counts[v]) for v, label in q["options"] if counts[v]]}
    numbers = {}
    for q in NUMBERS:
        values = [a[q["key"]] for a in answers if q["key"] in a]
        numbers[q["key"]] = {"label": q["label"], "unit": q["unit"],
                             "median": statistics.median(values) if values else None, "n": len(values)}
    return {"count": len(rows), "ratings": ratings, "choices": choices, "numbers": numbers}


def to_csv(rows: list[dict]) -> str:
    out = io.StringIO()
    w = csv.writer(out)
    cols = columns()
    w.writerow(["回答日時", "テストの回", *[label for _, label in cols]])
    for r in rows:
        w.writerow([r.get("created_at", ""), r.get("session_ref") or "", *[cell(r["answers"], k) for k, _ in cols]])
    return out.getvalue()


def load_from_site_db(path: Path) -> dict[int, dict]:
    """Discord ボットから、サイトのデータベースにあるフォームの答えを読む ({テストの回: 答え})。"""
    if not path.exists():
        return {}
    try:
        con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        try:
            rows = con.execute(
                "SELECT session_ref, answers FROM feedback WHERE session_ref IS NOT NULL ORDER BY id").fetchall()
        finally:
            con.close()
    except sqlite3.Error:
        return {}
    result: dict[int, dict] = {}
    for ref, raw in rows:
        try:
            result[int(ref)] = json.loads(raw)
        except (TypeError, ValueError):
            continue
    return result
