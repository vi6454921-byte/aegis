"""Экспорт: таблицы целиком в CSV («;», UTF-8 с BOM) и Excel-отчёт по пользователям и покупкам."""
from __future__ import annotations

import csv
from datetime import date, datetime
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Order, Payment, Transaction, User
from app.utils.timeutil import now_utc, to_local

TABLES = {"users": User, "orders": Order, "transactions": Transaction, "payments": Payment}


def _cell(value) -> str:  # noqa: ANN001
    if value is None:
        return ""
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, datetime):
        return to_local(value).strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


async def export_csv(session: AsyncSession, out_dir: Path) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = f"{to_local(now_utc()):%Y%m%d-%H%M%S}"
    paths = []
    for name, model in TABLES.items():
        cols = [c.name for c in model.__table__.columns]
        rows = (await session.execute(select(model).order_by(*model.__table__.primary_key.columns))).scalars()
        path = out_dir / f"{name}-{stamp}.csv"
        with open(path, "w", newline="", encoding="utf-8-sig") as fh:
            w = csv.writer(fh, delimiter=";")
            w.writerow(cols)
            for row in rows:
                w.writerow([_cell(getattr(row, c)) for c in cols])
        paths.append(path)
    return paths


# ------------------------------------------------------------------ Excel: пользователи и покупки
SUBJECT_NAMES = {"nach": "Начерталка", "calc": "Расчётка по вышмату", "explain": "Объяснение материала",
                 "any": "Любая задача", "design": "Файлы и дизайн"}


def _what(o: Order) -> str:
    """«Начерталка»; у «Файлов и дизайна» ещё тип, оформление и количество."""
    name = SUBJECT_NAMES.get(o.subject or "", o.subject or "")
    if o.subject == "design":
        from app.services.design import describe_plain  # noqa: PLC0415

        return f"{name}: {describe_plain(o)}"
    return name


STATUS_NAMES = {"estimate": "ждёт оценки", "quoted": "оценено", "review": "на проверке", "awaiting_payment": "ждёт оплаты", "paid": "оплачен", "in_work": "в работе",
                "done": "готово", "accepted": "зачтено", "dispute": "спор", "refunded": "возврат",
                "rejected": "отклонён", "cancelled": "отменён"}
CHANNEL_NAMES = {"member": "подписан", "left": "не подписан"}


def _status(o: Order) -> str:
    if o.subject == "design" and o.status == "accepted":  # v2.4: файл принят, а не «зачтено»
        return "принято"
    return STATUS_NAMES.get(o.status, o.status)


def _rub(kop: int | None) -> float:
    return round((kop or 0) / 100, 2)


def _dt(value: datetime | None) -> datetime | None:
    return to_local(value).replace(tzinfo=None) if value else None


async def users_report(session: AsyncSession) -> dict[str, tuple[list[str], list[list]]]:
    """Три листа: пользователи, покупки (оплаченные заказы), пополнения. Деньги - в рублях."""
    users = list((await session.execute(select(User).order_by(User.created_at, User.id))).scalars())
    by_id = {u.id: u for u in users}
    orders = list((await session.execute(select(Order).where(Order.paid_at.is_not(None))
                                         .order_by(Order.paid_at, Order.id))).scalars())
    topups = list((await session.execute(select(Payment).where(Payment.purpose == "topup", Payment.status == "paid")
                                         .order_by(Payment.paid_at, Payment.id))).scalars())
    stats: dict[int, dict] = {}
    for o in orders:
        st = stats.setdefault(o.user_id, {"n": 0, "rub": 0, "bonus": 0, "last": None, "what": ""})
        st["n"] += 1
        st["rub"] += (o.paid_balance or 0) + (o.paid_invoice or 0)
        st["bonus"] += o.paid_bonus or 0
        st["last"], st["what"] = o.paid_at, f"#{o.id} {_what(o)}"
    n_orders = dict((await session.execute(select(Order.user_id, func.count(Order.id))
                                           .where(Order.status != "draft").group_by(Order.user_id))).all())

    u_head = ["№", "ID", "Имя", "Username", "Телефон", "Регистрация", "Последний визит", "Источник",
              "Пригласил (ID)", "Канал", "Рубли, ₽", "Бонусы, ₽", "Заказов", "Оплачено заказов",
              "Заплатил деньгами, ₽", "Оплачено бонусами, ₽", "Последняя покупка", "Что купил последним",
              "Бан", "Заблокировал бота"]
    u_rows = []
    for i, u in enumerate(users, 1):
        st = stats.get(u.id, {})
        name = " ".join(p for p in (u.first_name, u.last_name) if p)
        u_rows.append([i, u.id, name, f"@{u.username}" if u.username else "", u.phone or "", _dt(u.created_at),
                       _dt(u.last_seen_at), u.source + (f":{u.source_tag}" if u.source_tag else ""),
                       u.referrer_id or "", CHANNEL_NAMES.get(u.channel_status or "", ""), _rub(u.balance_rub),
                       _rub(u.balance_bonus), int(n_orders.get(u.id, 0)), st.get("n", 0), _rub(st.get("rub", 0)),
                       _rub(st.get("bonus", 0)), _dt(st.get("last")), st.get("what", ""),
                       ("да" + (f": {u.ban_reason}" if u.ban_reason else "")) if u.is_banned else "",
                       "да" if u.bot_blocked else ""])
    o_head = ["№", "Дата оплаты", "Заказ", "ID клиента", "Username", "Имя", "Телефон", "Что", "Задач", "Дедлайн",
              "Цена, ₽", "Бонусами, ₽", "С баланса, ₽", "Счётом, ₽", "Статус", "Тест"]
    o_rows = []
    for i, o in enumerate(orders, 1):
        u = by_id.get(o.user_id)
        o_rows.append([i, _dt(o.paid_at), o.id, o.user_id, f"@{u.username}" if u and u.username else "",
                       u.first_name if u and u.first_name else "", (u.phone if u else "") or "",
                       _what(o), o.task_count or 0, _dt(o.deadline_at),
                       _rub(o.price_final), _rub(o.paid_bonus), _rub(o.paid_balance), _rub(o.paid_invoice),
                       _status(o), "да" if o.is_test else ""])
    t_head = ["№", "Дата", "ID клиента", "Username", "Телефон", "Сумма, ₽", "Бонус к пополнению, ₽", "Способ", "Тест"]
    t_rows = []
    for i, p in enumerate(topups, 1):
        u = by_id.get(p.user_id)
        t_rows.append([i, _dt(p.paid_at), p.user_id, f"@{u.username}" if u and u.username else "",
                       (u.phone if u else "") or "", _rub(p.amount_rub), _rub(p.topup_bonus), p.provider,
                       "да" if p.is_test else ""])
    return {"Пользователи": (u_head, u_rows), "Покупки": (o_head, o_rows), "Пополнения": (t_head, t_rows)}


async def export_users_xlsx(session: AsyncSession, out_dir: Path) -> Path:
    """Excel-файл (openpyxl). Если библиотеки нет - CSV того же содержания (лист «Пользователи»)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = f"{to_local(now_utc()):%Y%m%d-%H%M%S}"
    report = await users_report(session)
    try:
        from openpyxl import Workbook  # noqa: PLC0415
        from openpyxl.styles import Alignment, Font, PatternFill  # noqa: PLC0415
        from openpyxl.utils import get_column_letter  # noqa: PLC0415
    except ImportError:  # pragma: no cover
        head, rows = report["Пользователи"]
        path = out_dir / f"aegis-users-{stamp}.csv"
        with open(path, "w", newline="", encoding="utf-8-sig") as fh:
            w = csv.writer(fh, delimiter=";")
            w.writerow(head)
            for row in rows:
                w.writerow([_cell(v) for v in row])
        return path
    wb = Workbook()
    wb.remove(wb.active)
    money_cols = {"Рубли, ₽", "Бонусы, ₽", "Заплатил деньгами, ₽", "Оплачено бонусами, ₽", "Цена, ₽", "Бонусами, ₽",
                  "С баланса, ₽", "Счётом, ₽", "Сумма, ₽", "Бонус к пополнению, ₽"}
    for title, (head, rows) in report.items():
        ws = wb.create_sheet(title)
        ws.append(head)
        for row in rows:
            ws.append(row)
        for cell in ws[1]:
            cell.font = Font(bold=True, color="FFFFFF")
            cell.fill = PatternFill("solid", fgColor="4F46E5")
            cell.alignment = Alignment(vertical="center", wrap_text=True)
        for idx, name in enumerate(head, 1):
            letter = get_column_letter(idx)
            width = max([len(str(name))] + [len(str(r[idx - 1])) for r in rows[:500] if r[idx - 1] is not None])
            ws.column_dimensions[letter].width = min(max(width + 2, 8), 40)
            for cell in ws[letter][1:]:
                if name in money_cols:
                    cell.number_format = "#,##0.00"
                elif isinstance(cell.value, datetime):
                    cell.number_format = "DD.MM.YYYY HH:MM"
        ws.freeze_panes = "A2"
        if rows:
            ws.auto_filter.ref = ws.dimensions
    path = out_dir / f"aegis-users-{stamp}.xlsx"
    wb.save(path)
    return path
