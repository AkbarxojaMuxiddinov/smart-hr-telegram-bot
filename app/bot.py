import asyncio
from datetime import date, datetime
from hashlib import sha256
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.redis import RedisStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.bot_api import BackendAPIError, api_request
from app.db import SessionLocal, init_db
from app.models import Attendance, Task, User
from app.services import check_in, check_out, local_now, start_task, submit_task

router = Router()


class EmployeeFlow(StatesGroup):
    checkin_location = State()
    checkout_location = State()
    task_proof = State()


class AdminFlow(StatesGroup):
    employee_name = State()
    task_title = State()
    task_description = State()
    task_due_at = State()
    office_name = State()
    office_location = State()


home_keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="📍 Ishni boshlash")],
        [KeyboardButton(text="🏁 Ishni tugatish")],
        [KeyboardButton(text="📝 Vazifalar"), KeyboardButton(text="📊 Davomat")],
    ],
    resize_keyboard=True,
)
admin_home_keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="📍 Ishni boshlash")],
        [KeyboardButton(text="🏁 Ishni tugatish")],
        [KeyboardButton(text="📝 Vazifalar"), KeyboardButton(text="📊 Davomat")],
        [KeyboardButton(text="👨‍💼 Admin Panel")],
    ],
    resize_keyboard=True,
)

admin_panel_keyboard = InlineKeyboardMarkup(
    inline_keyboard=[
        [InlineKeyboardButton(text="➕ Yangi xodim qo‘shish", callback_data="admin:add_employee")],
        [InlineKeyboardButton(text="📝 Vazifa biriktirish", callback_data="admin:assign_task")],
        [InlineKeyboardButton(text="📍 Ofis sozlash", callback_data="admin:add_office")],
    ]
)


def telegram_user(db: Session, telegram_id: int) -> User | None:
    user = db.scalar(select(User).where(User.telegram_id == telegram_id, User.is_active.is_(True)))
    return user


def home_keyboard_for(user: User | None) -> ReplyKeyboardMarkup:
    return admin_home_keyboard if user and user.role == "admin" else home_keyboard


def load_telegram_user(telegram_id: int) -> User | None:
    with SessionLocal() as db:
        return telegram_user(db, telegram_id)


def admin_from_message(message: Message) -> User | None:
    if not message.from_user:
        return None
    user = load_telegram_user(message.from_user.id)
    return user if user and user.role == "admin" else None


def admin_from_callback(callback: CallbackQuery) -> User | None:
    if not callback.from_user:
        return None
    user = load_telegram_user(callback.from_user.id)
    return user if user and user.role == "admin" else None


@router.message(CommandStart())
async def start(message: Message, command: CommandObject) -> None:
    if not message.from_user:
        return
    with SessionLocal() as db:
        user = telegram_user(db, message.from_user.id)
        if user is None and command.args:
            user = db.scalar(select(User).where(User.invite_token_hash == sha256(command.args.encode()).hexdigest()))
            if user and user.is_active and user.invite_expires_at and user.invite_expires_at > local_now():
                if user.telegram_id not in (None, message.from_user.id):
                    user = None
                else:
                    user.telegram_id = message.from_user.id
                    user.invite_token_hash = None
                    user.invite_expires_at = None
                    db.commit()
            else:
                user = None
        if user is None:
            await message.answer("Botdan foydalanish uchun HR yuborgan taklif havolasini oching.")
            return
        await message.answer(f"Salom, {user.full_name}!", reply_markup=home_keyboard_for(user))


@router.message(F.text == "👨‍💼 Admin Panel")
async def show_admin_panel(message: Message) -> None:
    admin = admin_from_message(message)
    if not admin:
        await message.answer("Bu menyu faqat administratorlar uchun.")
        return
    await message.answer("Admin panel — kerakli amalni tanlang:", reply_markup=admin_panel_keyboard)


@router.callback_query(F.data == "admin:add_employee")
async def ask_employee_name(callback: CallbackQuery, state: FSMContext) -> None:
    if not admin_from_callback(callback):
        await callback.answer("Admin ruxsati talab qilinadi.", show_alert=True)
        return
    await state.clear()
    await state.set_state(AdminFlow.employee_name)
    await callback.answer()
    await callback.message.answer("Yangi xodimning ism-familiyasini yuboring. Bekor qilish: /cancel")


@router.callback_query(F.data == "admin:assign_task")
async def choose_task_assignee(callback: CallbackQuery, state: FSMContext) -> None:
    admin = admin_from_callback(callback)
    if not admin:
        await callback.answer("Admin ruxsati talab qilinadi.", show_alert=True)
        return
    await state.clear()
    try:
        employees = await api_request(admin, "GET", "/api/users", params={"role": "employee"})
    except BackendAPIError as error:
        await callback.answer(str(error), show_alert=True)
        return
    employees = [employee for employee in employees if employee.get("is_active")]
    if not employees:
        await callback.answer("Avval xodim qo‘shing.", show_alert=True)
        return
    keyboard = [
        [InlineKeyboardButton(text=employee["full_name"], callback_data=f"admin:assignee:{employee['id']}")]
        for employee in employees
    ]
    keyboard.append([InlineKeyboardButton(text="Bekor qilish", callback_data="admin:cancel")])
    await callback.answer()
    await callback.message.answer(
        "Vazifa biriktiriladigan xodimni tanlang:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard),
    )


@router.callback_query(F.data.startswith("admin:assignee:"))
async def select_task_assignee(callback: CallbackQuery, state: FSMContext) -> None:
    if not admin_from_callback(callback):
        await callback.answer("Admin ruxsati talab qilinadi.", show_alert=True)
        return
    try:
        employee_id = int(callback.data.rsplit(":", 1)[1])
    except (AttributeError, ValueError):
        await callback.answer("Xodim tanlovi noto‘g‘ri.", show_alert=True)
        return
    await state.clear()
    await state.update_data(assigned_to=employee_id)
    await state.set_state(AdminFlow.task_title)
    await callback.answer()
    await callback.message.answer("Vazifa nomini yuboring. Bekor qilish: /cancel")


@router.callback_query(F.data == "admin:add_office")
async def ask_office_name(callback: CallbackQuery, state: FSMContext) -> None:
    if not admin_from_callback(callback):
        await callback.answer("Admin ruxsati talab qilinadi.", show_alert=True)
        return
    await state.clear()
    await state.set_state(AdminFlow.office_name)
    await callback.answer()
    await callback.message.answer("Ofis nomini yuboring. Bekor qilish: /cancel")


@router.callback_query(F.data == "admin:cancel")
async def cancel_admin_callback(callback: CallbackQuery, state: FSMContext) -> None:
    if not admin_from_callback(callback):
        await callback.answer("Admin ruxsati talab qilinadi.", show_alert=True)
        return
    await state.clear()
    await callback.answer("Bekor qilindi.")
    await callback.message.answer("Admin panel.", reply_markup=admin_panel_keyboard)


@router.message(Command("cancel"), AdminFlow.employee_name)
@router.message(Command("cancel"), AdminFlow.task_title)
@router.message(Command("cancel"), AdminFlow.task_description)
@router.message(Command("cancel"), AdminFlow.task_due_at)
@router.message(Command("cancel"), AdminFlow.office_name)
@router.message(Command("cancel"), AdminFlow.office_location)
async def cancel_admin_flow(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Amal bekor qilindi.", reply_markup=admin_home_keyboard)


@router.message(AdminFlow.employee_name, F.text)
async def create_employee_from_admin(message: Message, state: FSMContext) -> None:
    admin = admin_from_message(message)
    name = (message.text or "").strip()
    if not admin:
        await state.clear()
        await message.answer("Admin ruxsati talab qilinadi.")
        return
    if len(name) < 2:
        await message.answer("Ism-familiya kamida 2 ta belgidan iborat bo‘lsin.")
        return
    try:
        result = await api_request(
            admin,
            "POST",
            "/api/users",
            json={"full_name": name, "role": "employee"},
        )
    except BackendAPIError as error:
        await message.answer(f"Xodim qo‘shilmadi: {error}")
        return
    token = result.get("invite_token")
    link = result.get("telegram_deep_link")
    invitation = f"Taklif havolasi: {link}" if link else f"Taklif kodi: /start {token}"
    await state.clear()
    await message.answer(
        f"Xodim qo‘shildi: {result['user']['full_name']}\n\n"
        f"Unga quyidagi taklifni yuboring: {invitation}\n"
        "Xodim botni ochib havolani bosishi yoki taklif kodini yuborishi kerak.",
        reply_markup=admin_home_keyboard,
    )


@router.message(AdminFlow.task_title, F.text)
async def receive_task_title(message: Message, state: FSMContext) -> None:
    title = (message.text or "").strip()
    if len(title) < 2:
        await message.answer("Vazifa nomi kamida 2 ta belgidan iborat bo‘lsin.")
        return
    await state.update_data(title=title)
    await state.set_state(AdminFlow.task_description)
    await message.answer("Vazifa tavsifini yuboring yoki o‘tkazib yuborish uchun /skip yozing.")


@router.message(Command("skip"), AdminFlow.task_description)
async def skip_task_description(message: Message, state: FSMContext) -> None:
    await state.update_data(description=None)
    await state.set_state(AdminFlow.task_due_at)
    await message.answer("Muddatni YYYY-MM-DD HH:MM formatida yuboring yoki muddat bo‘lmasa - yuboring.")


@router.message(AdminFlow.task_description, F.text)
async def receive_task_description(message: Message, state: FSMContext) -> None:
    description = (message.text or "").strip()
    await state.update_data(description=description or None)
    await state.set_state(AdminFlow.task_due_at)
    await message.answer("Muddatni YYYY-MM-DD HH:MM formatida yuboring yoki muddat bo‘lmasa - yuboring.")


@router.message(AdminFlow.task_due_at, F.text)
async def create_task_from_admin(message: Message, state: FSMContext) -> None:
    admin = admin_from_message(message)
    if not admin:
        await state.clear()
        await message.answer("Admin ruxsati talab qilinadi.")
        return
    due_text = (message.text or "").strip()
    due_at = None
    if due_text != "-":
        try:
            due_at = datetime.strptime(due_text, "%Y-%m-%d %H:%M").replace(
                tzinfo=ZoneInfo(settings.timezone)
            ).isoformat()
        except ValueError:
            await message.answer("Sana formati noto‘g‘ri. Masalan: 2026-11-20 17:30 yoki -")
            return
    data = await state.get_data()
    payload = {
        "title": data["title"],
        "description": data.get("description"),
        "assigned_to": data["assigned_to"],
        "due_at": due_at,
    }
    try:
        task = await api_request(admin, "POST", "/api/tasks", json=payload)
    except BackendAPIError as error:
        await state.clear()
        await message.answer(f"Vazifa biriktirilmadi: {error}", reply_markup=admin_home_keyboard)
        return
    await state.clear()
    await message.answer(
        f"Vazifa biriktirildi: #{task['id']} — {task['title']}",
        reply_markup=admin_home_keyboard,
    )


@router.message(AdminFlow.office_name, F.text)
async def receive_office_name(message: Message, state: FSMContext) -> None:
    name = (message.text or "").strip()
    if len(name) < 2:
        await message.answer("Ofis nomi kamida 2 ta belgidan iborat bo‘lsin.")
        return
    await state.update_data(office_name=name)
    await state.set_state(AdminFlow.office_location)
    await message.answer(
        "Ofisning joriy joylashuvini yuboring. Radius 100 m, ish boshlanishi 09:00 qilib belgilanadi.",
        reply_markup=ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text="📍 Ofis joylashuvini yuborish", request_location=True)]],
            resize_keyboard=True,
            one_time_keyboard=True,
        ),
    )


@router.message(AdminFlow.office_location, F.location)
async def create_office_from_admin(message: Message, state: FSMContext) -> None:
    admin = admin_from_message(message)
    if not admin or not message.location:
        await state.clear()
        await message.answer("Admin ruxsati talab qilinadi.", reply_markup=admin_home_keyboard)
        return
    data = await state.get_data()
    payload = {
        "name": data["office_name"],
        "latitude": message.location.latitude,
        "longitude": message.location.longitude,
        "radius_meters": 100,
        "work_start_time": "09:00:00",
    }
    try:
        office = await api_request(admin, "POST", "/api/offices", json=payload)
    except BackendAPIError as error:
        await state.clear()
        await message.answer(f"Ofis saqlanmadi: {error}", reply_markup=admin_home_keyboard)
        return
    await state.clear()
    await message.answer(
        f"Ofis sozlandi: {office['name']}\nRadius: {office['radius_meters']} m\nIsh boshlanishi: {office['work_start_time']}",
        reply_markup=admin_home_keyboard,
    )


@router.message(AdminFlow.office_location)
async def request_office_location(message: Message) -> None:
    await message.answer("Telegram’dagi 📍 Joylashuvni yuborish tugmasini bosing yoki /cancel yuboring.")


@router.message(Command("checkin"))
@router.message(F.text == "📍 Ishni boshlash")
async def ask_checkin_location(message: Message, state: FSMContext) -> None:
    await state.set_state(EmployeeFlow.checkin_location)
    await message.answer(
        "Check-in uchun joriy joylashuvingizni yuboring.",
        reply_markup=ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text="📍 Joylashuvni yuborish", request_location=True)]],
            resize_keyboard=True,
            one_time_keyboard=True,
        ),
    )


@router.message(Command("checkout"))
@router.message(F.text == "🏁 Ishni tugatish")
async def ask_checkout_location(message: Message, state: FSMContext) -> None:
    await state.set_state(EmployeeFlow.checkout_location)
    await message.answer(
        "Check-out uchun joriy joylashuvingizni yuboring.",
        reply_markup=ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text="📍 Joylashuvni yuborish", request_location=True)]],
            resize_keyboard=True,
            one_time_keyboard=True,
        ),
    )


@router.message(EmployeeFlow.checkin_location, F.location)
async def handle_checkin_location(message: Message, state: FSMContext) -> None:
    if not message.from_user or not message.location:
        return
    with SessionLocal() as db:
        user = telegram_user(db, message.from_user.id)
        if not user:
            await message.answer("Foydalanuvchi faol emas. HR bilan bog‘laning.", reply_markup=home_keyboard_for(user))
            await state.clear()
            return
        try:
            record = check_in(db, user, message.location.latitude, message.location.longitude)
            result = f"Ish boshlandi: {record.check_in_time:%H:%M}."
            if record.late_minutes:
                result += f" Kechikish: {record.late_minutes} daqiqa."
            else:
                result += " O‘z vaqtida."
            await message.answer(result, reply_markup=home_keyboard_for(user))
        except HTTPException as error:
            await message.answer(error.detail, reply_markup=home_keyboard_for(user))
    await state.clear()


@router.message(EmployeeFlow.checkout_location, F.location)
async def handle_checkout_location(message: Message, state: FSMContext) -> None:
    if not message.from_user or not message.location:
        return
    with SessionLocal() as db:
        user = telegram_user(db, message.from_user.id)
        if not user:
            await message.answer("Foydalanuvchi faol emas. HR bilan bog‘laning.", reply_markup=home_keyboard_for(user))
            await state.clear()
            return
        try:
            record = check_out(db, user, message.location.latitude, message.location.longitude)
            await message.answer(f"Ish kuni {record.check_out_time:%H:%M} da tugatildi.", reply_markup=home_keyboard_for(user))
        except HTTPException as error:
            await message.answer(error.detail, reply_markup=home_keyboard_for(user))
    await state.clear()


@router.message(Command("tasks"))
@router.message(F.text == "📝 Vazifalar")
async def show_tasks(message: Message) -> None:
    if not message.from_user:
        return
    with SessionLocal() as db:
        user = telegram_user(db, message.from_user.id)
        if not user:
            await message.answer("HR yuborgan taklif havolasi orqali ro‘yxatdan o‘ting.")
            return
        tasks = list(db.scalars(
            select(Task).where(Task.assigned_to == user.id).order_by(Task.created_at.desc()).limit(20)
        ))
        if not tasks:
            await message.answer("Sizga vazifalar biriktirilmagan.", reply_markup=home_keyboard_for(user))
            return
        lines = ["Sizning vazifalaringiz:"]
        for task in tasks:
            description = f"\n{task.description}" if task.description else ""
            review = f" · tekshiruv: {task.review_status}" if task.review_status != "not_submitted" else ""
            lines.append(f"\n#{task.id} — {task.title} [{task.status}{review}]{description}")
            if task.status == "pending":
                lines.append(f"\nBoshlash: /starttask {task.id}")
            elif task.status == "in_progress" or task.review_status == "rejected":
                lines.append(f"\nBajarilganda: /done {task.id}")
        await message.answer("".join(lines), reply_markup=home_keyboard_for(user))


@router.message(Command("starttask"))
async def begin_task(message: Message, command: CommandObject) -> None:
    if not message.from_user:
        return
    try:
        task_id = int((command.args or "").strip())
    except ValueError:
        await message.answer("Buyruq formati: /starttask 12")
        return
    with SessionLocal() as db:
        user = telegram_user(db, message.from_user.id)
        task = db.get(Task, task_id)
        if not user or not task:
            await message.answer("Vazifa topilmadi.")
            return
        try:
            start_task(db, task, user)
            await message.answer(f"Vazifa boshlandi: {task.title}", reply_markup=home_keyboard_for(user))
        except HTTPException as error:
            await message.answer(error.detail, reply_markup=home_keyboard_for(user))


@router.message(Command("done"))
async def begin_task_submission(message: Message, command: CommandObject, state: FSMContext) -> None:
    if not message.from_user:
        return
    try:
        task_id = int((command.args or "").strip())
    except ValueError:
        await message.answer("Buyruq formati: /done 12")
        return
    with SessionLocal() as db:
        user = telegram_user(db, message.from_user.id)
        task = db.get(Task, task_id)
        if not user or not task or task.assigned_to != user.id:
            await message.answer("Vazifa topilmadi.")
            return
        if task.status == "completed":
            await message.answer("Vazifa allaqachon topshirilgan.")
            return
    await state.set_state(EmployeeFlow.task_proof)
    await state.update_data(task_id=task_id)
    await message.answer("Vazifaga dalil sifatida rasm, fayl yoki izoh yuboring. Bekor qilish: /cancel", reply_markup=ReplyKeyboardRemove())


@router.message(Command("cancel"), EmployeeFlow.task_proof)
async def cancel_submission(message: Message, state: FSMContext) -> None:
    await state.clear()
    user = load_telegram_user(message.from_user.id) if message.from_user else None
    await message.answer("Yuborish bekor qilindi.", reply_markup=home_keyboard_for(user))


@router.message(EmployeeFlow.task_proof)
async def receive_task_proof(message: Message, state: FSMContext) -> None:
    if not message.from_user:
        return
    file_id = None
    comment = message.caption
    if message.photo:
        file_id = message.photo[-1].file_id
    elif message.document:
        file_id = message.document.file_id
    elif message.text:
        comment = message.text.strip()
    else:
        await message.answer("Rasm, fayl yoki matnli izoh yuboring.")
        return
    if not file_id and not comment:
        await message.answer("Iltimos, faylga izoh qo‘shing yoki matn yuboring.")
        return

    state_data = await state.get_data()
    with SessionLocal() as db:
        user = telegram_user(db, message.from_user.id)
        task = db.get(Task, state_data.get("task_id"))
        if not user or not task:
            await message.answer("Xodim yoki vazifa topilmadi.", reply_markup=home_keyboard_for(user))
        else:
            try:
                submit_task(db, task, user, file_id, comment)
                await message.answer("Vazifa yuborildi. HR tasdiqlashini kuting.", reply_markup=home_keyboard_for(user))
            except HTTPException as error:
                await message.answer(error.detail, reply_markup=home_keyboard_for(user))
    await state.clear()


@router.message(Command("attendance"))
@router.message(F.text == "📊 Davomat")
async def show_monthly_attendance(message: Message) -> None:
    if not message.from_user:
        return
    today = local_now().date()
    first = date(today.year, today.month, 1)
    next_month = date(today.year + (today.month == 12), 1 if today.month == 12 else today.month + 1, 1)
    with SessionLocal() as db:
        user = telegram_user(db, message.from_user.id)
        if not user:
            await message.answer("HR yuborgan taklif havolasi orqali ro‘yxatdan o‘ting.")
            return
        rows = list(db.scalars(select(Attendance).where(
            Attendance.user_id == user.id,
            Attendance.work_date >= first,
            Attendance.work_date < next_month,
        )))
    late_days = sum(row.status == "late" for row in rows)
    late_minutes = sum(row.late_minutes for row in rows)
    await message.answer(
        f"{today:%Y-%m} davomat:\nKelgan kunlar: {len(rows)}\nKechikkan kunlar: {late_days}\nJami kechikish: {late_minutes} daqiqa",
        reply_markup=home_keyboard_for(user),
    )


async def main() -> None:
    if not settings.telegram_bot_token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN sozlanmagan")
    init_db()
    bot = Bot(settings.telegram_bot_token)
    dispatcher = Dispatcher(storage=RedisStorage.from_url(settings.redis_url))
    dispatcher.include_router(router)
    await dispatcher.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
