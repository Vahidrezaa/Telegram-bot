import os
import logging
import uuid
import asyncio
from datetime import datetime, timedelta
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    Bot,
    constants
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
    ConversationHandler
)
from dotenv import load_dotenv
import aiohttp
from aiohttp import web

# تنظیمات محیطی
load_dotenv()
BOT_TOKEN = os.getenv('BOT_TOKEN')
ADMIN_IDS = [int(id) for id in os.getenv('ADMIN_IDS', '').split(',') if id]
STORAGE_CHANNELS = [chan.strip() for chan in os.getenv('STORAGE_CHANNELS', '').split(',') if chan.strip()]
DEFAULT_TIMER = int(os.getenv('DEFAULT_TIMER', 3600))  # زمان پیش‌فرض: 1 ساعت

# تنظیمات لاگ
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# حالت‌های گفتگو
UPLOADING, WAITING_CHANNEL_INFO, WAITING_TIMER, WAITING_CATEGORY_TIMER = range(4)

class ChannelStorage:
    """سیستم ذخیره‌سازی بهینه‌شده در کانال تلگرام"""
    
    def __init__(self, bot):
        self.bot = bot
        self.channels = STORAGE_CHANNELS
        self.categories_per_message = 10
        self.global_timer = DEFAULT_TIMER
        self.category_timers = {}
        self.current_channel_index = 0
        self.message_cache = {}
        self.loaded = False
    
    async def initialize(self):
        """بارگذاری اولیه داده‌ها"""
        if self.loaded:
            return
            
        # بارگذاری تایمر جهانی
        await self.load_global_timer()
        
        # بارگذاری تایمرهای اختصاصی دسته‌ها
        await self.load_category_timers()
        
        self.loaded = True
        logger.info("Storage initialized")
    
    async def load_global_timer(self):
        """بارگذاری تایمر جهانی از کانال ذخیره‌سازی"""
        for channel in self.channels:
            try:
                # دریافت تاریخچه چت با استفاده از روش صحیح PTB
                messages = []
                async for message in self.bot.get_chat_history(chat_id=channel, limit=100):
                    if message.text and "===== GLOBAL TIMER =====" in message.text:
                        try:
                            self.global_timer = int(message.text.split('\n')[1])
                            return
                        except (IndexError, ValueError):
                            pass
            except Exception as e:
                logger.error(f"خطا در بارگذاری تایمر جهانی: {e}")
    
    async def load_category_timers(self):
        """بارگذاری تایمرهای اختصاصی دسته‌ها"""
        for channel in self.channels:
            try:
                # دریافت تاریخچه چت با استفاده از روش صحیح PTB
                async for message in self.bot.get_chat_history(chat_id=channel, limit=100):
                    if message.text and "===== META =====" in message.text:
                        lines = message.text.split('\n')
                        category_id = None
                        
                        for line in lines:
                            if line.startswith("CATEGORY:"):
                                category_id = line.split(':')[1]
                            elif line.startswith("TIMER:") and category_id:
                                try:
                                    self.category_timers[category_id] = int(line.split(':')[1])
                                except ValueError:
                                    pass
            except Exception as e:
                logger.error(f"خطا در بارگذاری تایمرهای دسته: {e}")
    
    async def save_global_timer(self, seconds: int):
        """ذخیره تایمر جهانی در کانال ذخیره‌سازی"""
        self.global_timer = seconds
        
        # حذف تایمرهای قدیمی
        for channel in self.channels:
            try:
                async for message in self.bot.get_chat_history(chat_id=channel, limit=100):
                    if message.text and "===== GLOBAL TIMER =====" in message.text:
                        await message.delete()
            except Exception as e:
                logger.error(f"خطا در حذف تایمر قدیمی: {e}")
        
        # ذخیره تایمر جدید
        if self.channels:
            await self.bot.send_message(
                chat_id=self.channels[0],
                text=f"===== GLOBAL TIMER =====\n{seconds}"
            )
    
    async def save_category_timer(self, category_id: str, seconds: int):
        """ذخیره تایمر اختصاصی برای یک دسته"""
        self.category_timers[category_id] = seconds
        
        # پیدا کردن پیام دسته و به‌روزرسانی آن
        for channel in self.channels:
            try:
                async for message in self.bot.get_chat_history(chat_id=channel, limit=100):
                    if message.text and f"CATEGORY:{category_id}" in message.text:
                        lines = message.text.split('\n')
                        new_lines = []
                        timer_found = False
                        
                        for line in lines:
                            if line.startswith("TIMER:"):
                                new_lines.append(f"TIMER:{seconds}")
                                timer_found = True
                            else:
                                new_lines.append(line)
                        
                        if not timer_found:
                            # اگر خط تایمر وجود نداشت، آن را اضافه کن
                            for i, line in enumerate(new_lines):
                                if line.startswith("CREATED_BY:"):
                                    new_lines.insert(i + 1, f"TIMER:{seconds}")
                                    break
                        
                        await message.edit_text('\n'.join(new_lines))
                        return
            except Exception as e:
                logger.error(f"خطا در به‌روزرسانی تایمر دسته: {e}")
    
    async def _find_message_for_category(self, category_id: str = None):
        """پیدا کردن پیام مناسب برای دسته"""
        for channel in self.channels:
            try:
                async for message in self.bot.get_chat_history(chat_id=channel, limit=100):
                    if message.text and message.text.startswith("CATEGORIES_BLOCK:"):
                        categories = message.text.split('\n')[1:]
                        
                        for i, cat_line in enumerate(categories):
                            if cat_line.startswith(f"CATEGORY:{category_id}" if category_id else "CATEGORY:"):
                                return message, i, channel
                        
                        # اگر پیام پیدا شد اما جای خالی دارد
                        if len(categories) < self.categories_per_message:
                            return message, len(categories), channel
            except Exception as e:
                logger.error(f"خطا در جستجوی پیام دسته: {e}")
        
        return None, None, None
    
    async def add_category(self, name: str, created_by: int) -> str:
        """ایجاد دسته جدید"""
        category_id = str(uuid.uuid4())[:8]
        category_data = (
            f"CATEGORY:{category_id}\n"
            f"NAME:{name}\n"
            f"CREATED_BY:{created_by}\n"
            f"TIMER:{self.global_timer}\n"
            "FILES:"
        )
        
        message, pos, channel = await self._find_message_for_category()
        
        if message:
            # افزودن به پیام موجود
            lines = message.text.split('\n')
            lines.insert(pos + 1, category_data)
            
            # بررسی اندازه پیام
            new_text = '\n'.join(lines)
            if len(new_text) <= 4096:
                await self.bot.edit_message_text(
                    chat_id=channel,
                    message_id=message.message_id,
                    text=new_text
                )
            else:
                # اگر پیام پر شد، پیام جدید ایجاد کنید
                message = None
        else:
            # پیام جدید ایجاد شود
            message = None
        
        if not message:
            new_text = f"CATEGORIES_BLOCK:\n{category_data}"
            # چرخش بین کانال‌ها برای توزیع بار
            channel = self.channels[self.current_channel_index]
            self.current_channel_index = (self.current_channel_index + 1) % len(self.channels)
            
            await self.bot.send_message(
                chat_id=channel,
                text=new_text
            )
        
        # ذخیره تایمر در کش
        self.category_timers[category_id] = self.global_timer
        
        return category_id
    
    async def get_categories(self) -> dict:
        """دریافت تمام دسته‌ها"""
        categories = {}
        for channel in self.channels:
            try:
                async for message in self.bot.get_chat_history(chat_id=channel, limit=100):
                    if message.text and message.text.startswith("CATEGORIES_BLOCK:"):
                        for line in message.text.split('\n')[1:]:
                            if line.startswith("CATEGORY:"):
                                cat_id = line.split(':')[1]
                                # خط بعدی نام دسته است
                                name_line = message.text.split('\n')[message.text.split('\n').index(line) + 1]
                                name = name_line.split(':')[1]
                                categories[cat_id] = name
            except Exception as e:
                logger.error(f"خطا در دریافت دسته‌ها: {e}")
        return categories
    
    async def get_category(self, category_id: str) -> dict:
        """دریافت اطلاعات یک دسته"""
        for channel in self.channels:
            try:
                async for message in self.bot.get_chat_history(chat_id=channel, limit=100):
                    if message.text and f"CATEGORY:{category_id}" in message.text:
                        lines = message.text.split('\n')
                        
                        # پیدا کردن موقعیت دسته
                        start_idx = None
                        for i, line in enumerate(lines):
                            if line.startswith(f"CATEGORY:{category_id}"):
                                start_idx = i
                                break
                        
                        if start_idx is None:
                            continue
                        
                        # استخراج اطلاعات
                        name = lines[start_idx + 1].split(':')[1]
                        
                        # استخراج تایمر اگر وجود دارد
                        timer = self.global_timer
                        if lines[start_idx + 2].startswith("TIMER:"):
                            try:
                                timer = int(lines[start_idx + 2].split(':')[1])
                            except (IndexError, ValueError):
                                pass
                        
                        files = []
                        file_lines = lines[start_idx + 4:]  # خطوط بعد از FILES:
                        for line in file_lines:
                            if line and not line.startswith("CATEGORY:"):
                                file_data = line.split('|')
                                if len(file_data) >= 2:
                                    files.append({
                                        'file_id': file_data[0],
                                        'file_type': file_data[1],
                                        'caption': file_data[2] if len(file_data) > 2 else ''
                                    })
                            else:
                                break
                        
                        return {
                            'name': name,
                            'timer': timer,
                            'files': files
                        }
            except Exception as e:
                logger.error(f"خطا در دریافت اطلاعات دسته: {e}")
        return None
    
    async def add_file(self, category_id: str, file_info: dict) -> bool:
        """افزودن فایل به دسته"""
        for channel in self.channels:
            try:
                async for message in self.bot.get_chat_history(chat_id=channel, limit=100):
                    if message.text and f"CATEGORY:{category_id}" in message.text:
                        lines = message.text.split('\n')
                        
                        # پیدا کردن موقعیت FILES: برای این دسته
                        files_idx = None
                        for i, line in enumerate(lines):
                            if line.startswith(f"CATEGORY:{category_id}"):
                                # پیدا کردن خط FILES: بعد از این دسته
                                for j in range(i, len(lines)):
                                    if lines[j].startswith("FILES:"):
                                        files_idx = j
                                        break
                                break
                        
                        if files_idx is None:
                            continue
                        
                        # افزودن فایل جدید
                        new_file_line = f"{file_info['file_id']}|{file_info['file_type']}|{file_info.get('caption', '')}"
                        lines.insert(files_idx + 1, new_file_line)
                        
                        # بررسی اندازه پیام
                        new_text = '\n'.join(lines)
                        if len(new_text) > 4096:
                            continue  # به پیام بعدی برو
                        
                        await message.edit_text(new_text)
                        return True
            except Exception as e:
                logger.error(f"خطا در افزودن فایل: {e}")
        return False
    
    async def delete_category(self, category_id: str) -> bool:
        """حذف یک دسته"""
        for channel in self.channels:
            try:
                async for message in self.bot.get_chat_history(chat_id=channel, limit=100):
                    if message.text and f"CATEGORY:{category_id}" in message.text:
                        lines = message.text.split('\n')
                        
                        # پیدا کردن محدوده خطوط این دسته
                        start_idx = None
                        end_idx = None
                        for i, line in enumerate(lines):
                            if line.startswith(f"CATEGORY:{category_id}"):
                                start_idx = i
                            elif start_idx is not None and i > start_idx and line.startswith("CATEGORY:"):
                                end_idx = i
                                break
                        
                        if start_idx is None:
                            continue
                        
                        # حذف خطوط مربوط به این دسته
                        if end_idx:
                            del lines[start_idx:end_idx]
                        else:
                            del lines[start_idx:]
                        
                        # حذف تایمر از کش
                        if category_id in self.category_timers:
                            del self.category_timers[category_id]
                        
                        # اگر پیام خالی شد، آن را حذف کنید
                        if len(lines) <= 1:  # فقط خط CATEGORIES_BLOCK: باقی مانده
                            await message.delete()
                        else:
                            await message.edit_text('\n'.join(lines))
                        return True
            except Exception as e:
                logger.error(f"خطا در حذف دسته: {e}")
        return False

class BotManager:
    """مدیریت اصلی ربات"""
    
    def __init__(self):
        self.storage = None
        self.pending_uploads = {}
        self.pending_channels = {}
        self.pending_timers = {}
        self.bot_username = None
        self.delete_tasks = {}
    
    async def init(self, bot_username: str, bot):
        """راه‌اندازی اولیه"""
        self.bot_username = bot_username
        self.storage = ChannelStorage(bot)
        await self.storage.initialize()
    
    def is_admin(self, user_id: int) -> bool:
        """بررسی ادمین بودن کاربر"""
        return user_id in ADMIN_IDS
    
    def generate_link(self, category_id: str) -> str:
        """تولید لینک دسته با یوزرنیم صحیح"""
        if self.bot_username:
            return f"https://t.me/{self.bot_username}?start=cat_{category_id}"
        # Fallback در صورت عدم وجود یوزرنیم
        bot_id = BOT_TOKEN.split(':')[0]
        return f"https://t.me/{bot_id}?start=cat_{category_id}"
    
    def extract_file_info(self, update: Update) -> dict:
        msg = update.message

        if msg.document:
            file = msg.document
            file_type = 'document'
            file_name = file.file_name or f"document_{file.file_id[:8]}"
        elif msg.photo:
            file = msg.photo[-1]  # بالاترین کیفیت
            file_type = 'photo'
            file_name = f"photo_{file.file_id[:8]}.jpg"
        elif msg.video:
            file = msg.video
            file_type = 'video'
            file_name = f"video_{file.file_id[:8]}.mp4"
        elif msg.audio:
            file = msg.audio
            file_type = 'audio'
            file_name = f"audio_{file.file_id[:8]}.mp3"
        else:
            return None

        return {
            'file_id': file.file_id,
            'file_name': file_name,
            'file_size': file.file_size,
            'file_type': file_type,
            'caption': msg.caption or ''
        }

# ایجاد نمونه
bot_manager = BotManager()

# ========================
# ==== HANDLER FUNCTIONS ===
# ========================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """نسخه اصلاح شده دستور شروع"""
    user_id = update.effective_user.id
    
    # دسترسی از طریق لینک دسته
    if context.args and context.args[0].startswith('cat_'):
        category_id = context.args[0][4:]
        await handle_category(update, context, category_id)
        return
    
    if bot_manager.is_admin(user_id):
        # دریافت تایمر جهانی
        global_timer = bot_manager.storage.global_timer
        timer_status = f"{global_timer} ثانیه" if global_timer > 0 else "غیرفعال"
        
        await update.message.reply_text(
            "👋 سلام ادمین!\n\n"
            "دستورات:\n"
            "/new_category - ساخت دسته جدید\n"
            "/upload - شروع آپلود فایل\n"
            "/finish_upload - پایان آپلود\n"
            "/categories - نمایش دسته‌ها\n"
            "/add_channel - افزودن کانال اجباری\n"
            "/remove_channel - حذف کانال\n"
            "/channels - لیست کانال‌ها\n"
            f"/timer [زمان] - تنظیم تایمر جهانی (فعلی: {timer_status})"
        )
    else:
        await update.message.reply_text("👋 سلام! برای دریافت فایل‌ها از لینک‌ها استفاده کنید.")

async def is_user_member(context, channel_id, user_id):
    """بررسی عضویت کاربر با تلاش مجدد"""
    for _ in range(3):  # 3 بار تلاش
        try:
            member = await context.bot.get_chat_member(chat_id=channel_id, user_id=user_id)
            if member.status in ['member', 'administrator', 'creator']:
                return True
        except Exception as e:
            logger.warning(f"خطا در بررسی عضویت: {e}")
        
        await asyncio.sleep(2)  # تاخیر 2 ثانیه‌ای بین هر تلاش
    
    return False

async def handle_category(update: Update, context: ContextTypes.DEFAULT_TYPE, category_id: str):
    """مدیریت دسترسی به دسته"""
    # استخراج user_id و message بسته به نوع update
    if update.message:
        user_id = update.message.from_user.id
        message = update.message
    elif update.callback_query:
        user_id = update.callback_query.from_user.id
        message = update.callback_query.message
    else:
        logger.error("Unsupported update type")
        return

    # بررسی ادمین
    if bot_manager.is_admin(user_id):
        await admin_category_menu(message, context, category_id)
        return
    
    # بررسی عضویت در کانال‌ها
    # (کد قبلی بدون تغییر)

async def admin_category_menu(message: Message, context: ContextTypes.DEFAULT_TYPE, category_id: str):
    """منوی مدیریت دسته برای ادمین"""
    try:
        category = await bot_manager.storage.get_category(category_id)
        if not category:
            await message.reply_text("❌ دسته یافت نشد!")
            return
        
        # دریافت تایمر اختصاصی
        timer = bot_manager.storage.get_category_timer(category_id)
        timer_status = f"⏱ تایمر: {timer} ثانیه" if timer > 0 else "⏱ تایمر: غیرفعال"
        
        keyboard = [
            [InlineKeyboardButton("📁 مشاهده فایل‌ها", callback_data=f"view_{category_id}")],
            [InlineKeyboardButton("➕ افزودن فایل", callback_data=f"add_{category_id}")],
            [InlineKeyboardButton(timer_status, callback_data=f"timer_{category_id}")],
            [InlineKeyboardButton("🗑 حذف دسته", callback_data=f"delcat_{category_id}")]
        ]
        
        await message.reply_text(
            f"📂 دسته: {category['name']}\n"
            f"📦 تعداد فایل‌ها: {len(category['files'])}\n"
            f"{timer_status}\n\n"
            "لطفا عملیات مورد نظر را انتخاب کنید:",
            reply_markup=InlineKeyboardMarkup(keyboard))
    except Exception as e:
        logger.error(f"خطا در منوی ادمین: {e}")
        await message.reply_text("❌ خطایی در نمایش منو رخ داد")

async def send_category_files(message: Message, context: ContextTypes.DEFAULT_TYPE, category_id: str):
    """ارسال فایل‌های یک دسته با سیستم تایمر"""
    try:
        chat_id = message.chat_id
        user_id = message.from_user.id if message.from_user else message.chat_id
        
        category = await bot_manager.storage.get_category(category_id)
        if not category or not category['files']:
            await message.reply_text("❌ فایلی برای نمایش وجود ندارد!")
            return
        
        # تعیین تایمر مناسب
        timer = bot_manager.storage.get_category_timer(category_id)
        
        # ارسال فایل‌ها
        sent_messages = []
        await message.reply_text(f"📤 ارسال فایل‌های '{category['name']}'...")
        
        for file in category['files']:
            try:
                send_func = {
                    'document': context.bot.send_document,
                    'photo': context.bot.send_photo,
                    'video': context.bot.send_video,
                    'audio': context.bot.send_audio
                }.get(file['file_type'])
                
                if send_func:
                    sent_msg = await send_func(
                        chat_id=chat_id,
                        **{file['file_type']: file['file_id']},
                        caption=file.get('caption', '')[:1024]
                    )
                    sent_messages.append(sent_msg.message_id)
                await asyncio.sleep(0.5)  # افزایش تاخیر برای جلوگیری از محدودیت
            except Exception as e:
                logger.error(f"ارسال فایل خطا: {e}")
                await asyncio.sleep(2)
        
        # ارسال هشدار تایمر
        if timer > 0:
            warning_msg = await message.reply_text(
                f"⚠️ فایل‌ها بعد از {timer} ثانیه به صورت خودکار حذف خواهند شد!\n"
                f"زمان باقیمانده: {timer} ثانیه"
            )
            sent_messages.append(warning_msg.message_id)
            
            # زمان‌بندی برای حذف خودکار
            bot_manager.delete_tasks[user_id] = asyncio.create_task(delete_messages_after_delay(context, chat_id, sent_messages, timer))
        else:
            await message.reply_text("✅ فایل‌ها با موفقیت ارسال شدند.")
    except Exception as e:
        logger.error(f"خطا در ارسال فایل‌ها: {e}")
        await message.reply_text("❌ خطایی در ارسال فایل‌ها رخ داد")

async def delete_messages_after_delay(context: ContextTypes.DEFAULT_TYPE, chat_id: int, message_ids: list, delay: int):
    """نسخه اصلاح شده با مدیریت خطاهای بهتر"""
    try:
        remaining = delay
        while remaining > 0:
            await asyncio.sleep(min(10, remaining))
            remaining -= 10
            
            try:
                # به‌روزرسانی پیام هشدار
                if message_ids:
                    await context.bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=message_ids[-1],
                        text=f"⚠️ فایل‌ها بعد از {remaining} ثانیه حذف می‌شوند!\nزمان باقیمانده: {remaining} ثانیه"
                    )
            except Exception as e:
                logger.warning(f"خطا در به‌روزرسانی تایمر: {e}")

        # حذف پیام‌ها
        for msg_id in message_ids:
            try:
                await context.bot.delete_message(
                    chat_id=chat_id,
                    message_id=msg_id
                )
            except Exception as e:
                logger.warning(f"حذف پیام ناموفق: {e}")
                
    except asyncio.CancelledError:
        logger.info("حذف پیام‌ها لغو شد")
    except Exception as e:
        logger.error(f"خطای غیرمنتظره در تایمر حذف: {e}")

# ========================
# ==== ADMIN COMMANDS ====
# ========================

async def new_category(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """ایجاد دسته جدید"""
    user_id = update.effective_user.id
    if not bot_manager.is_admin(user_id):
        await update.message.reply_text("❌ دسترسی ممنوع!")
        return
    
    if not context.args:
        await update.message.reply_text("لطفا نام دسته را وارد کنید.\nمثال: /new_category نام_دسته")
        return
    
    name = ' '.join(context.args)
    category_id = await bot_manager.storage.add_category(name, user_id)
    link = bot_manager.generate_link(category_id)
    
    await update.message.reply_text(
        f"✅ دسته '{name}' ایجاد شد!\n\n"
        f"🔗 لینک دسته:\n{link}\n\n"
        f"تایمر فعلی: {bot_manager.storage.global_timer} ثانیه\n"
        f"برای آپلود فایل:\n/upload {category_id}")

async def upload_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """شروع آپلود فایل"""
    user_id = update.effective_user.id
    if not bot_manager.is_admin(user_id):
        await update.message.reply_text("❌ دسترسی ممنوع!")
        return
    
    if not context.args:
        await update.message.reply_text("لطفا آیدی دسته را مشخص کنید.\nمثال: /upload CAT_ID")
        return
    
    category_id = context.args[0]
    category = await bot_manager.storage.get_category(category_id)
    if not category:
        await update.message.reply_text("❌ دسته یافت نشد!")
        return
    
    bot_manager.pending_uploads[user_id] = {
        'category_id': category_id,
        'files': []
    }
    
    await update.message.reply_text(
        f"📤 حالت آپلود فعال شد! فایل‌ها را ارسال کنید.\n"
        f"برای پایان: /finish_upload\n"
        f"برای لغو: /cancel")
    return UPLOADING

async def handle_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """پردازش فایل‌های ارسالی"""
    user_id = update.effective_user.id
    if user_id not in bot_manager.pending_uploads:
        return
    
    file_info = bot_manager.extract_file_info(update)
    if not file_info:
        await update.message.reply_text("❌ نوع فایل پشتیبانی نمی‌شود!")
        return
    
    upload = bot_manager.pending_uploads[user_id]
    upload['files'].append(file_info)
    
    await update.message.reply_text(f"✅ فایل دریافت شد! (تعداد: {len(upload['files'])})")

async def finish_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """پایان آپلود فایل‌ها"""
    user_id = update.effective_user.id
    if user_id not in bot_manager.pending_uploads:
        await update.message.reply_text("❌ هیچ آپلودی فعال نیست!")
        return ConversationHandler.END
    
    upload = bot_manager.pending_uploads.pop(user_id)
    if not upload['files']:
        await update.message.reply_text("❌ فایلی دریافت نشد!")
        return ConversationHandler.END
    
    # افزودن فایل‌ها به ذخیره‌سازی
    added_count = 0
    for file in upload['files']:
        if await bot_manager.storage.add_file(upload['category_id'], file):
            added_count += 1
    
    link = bot_manager.generate_link(upload['category_id'])
    category = await bot_manager.storage.get_category(upload['category_id'])
    timer = bot_manager.storage.get_category_timer(upload['category_id'])
    
    await update.message.reply_text(
        f"✅ {added_count} فایل با موفقیت ذخیره شد!\n\n"
        f"🔗 لینک دسته:\n{link}\n"
        f"📂 نام دسته: {category['name']}\n"
        f"⏱ تایمر حذف: {timer} ثانیه")
    return ConversationHandler.END

async def categories_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """نمایش لیست دسته‌ها"""
    if not bot_manager.is_admin(update.effective_user.id):
        await update.message.reply_text("❌ دسترسی ممنوع!")
        return
    
    categories = await bot_manager.storage.get_categories()
    if not categories:
        await update.message.reply_text("📂 هیچ دسته‌ای وجود ندارد!")
        return
    
    message = "📁 لیست دسته‌ها:\n\n"
    for cid, name in categories.items():
        timer = bot_manager.storage.get_category_timer(cid)
        timer_info = f"⏱ {timer} ثانیه" if timer > 0 else "⏱ غیرفعال"
        message += f"• {name} [ID: {cid}] - {timer_info}\n"
        message += f"  لینک: {bot_manager.generate_link(cid)}\n\n"
    
    message += f"\n⏱ تایمر جهانی: {bot_manager.storage.global_timer} ثانیه"
    await update.message.reply_text(message)

# ========================
# === TIMER MANAGEMENT ===
# ========================

async def set_timer_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """تنظیم تایمر جهانی"""
    user_id = update.effective_user.id
    if not bot_manager.is_admin(user_id):
        await update.message.reply_text("❌ دسترسی ممنوع!")
        return
    
    try:
        seconds = int(context.args[0])
        if seconds < 0:
            seconds = 0
        
        await bot_manager.storage.save_global_timer(seconds)
        bot_manager.storage.global_timer = seconds
        
        await update.message.reply_text(
            f"✅ تایمر جهانی بر روی {seconds} ثانیه تنظیم شد.\n"
            f"این تایمر برای دسته‌های جدید و دسته‌هایی که تایمر اختصاصی ندارند اعمال می‌شود.")
    except (IndexError, ValueError):
        await update.message.reply_text("❌ لطفا زمان را به ثانیه وارد کنید.\nمثال: /timer 3600")

# ========================
# === BUTTON HANDLERS ====
# ========================

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """مدیریت کلیک روی دکمه‌ها"""
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = query.from_user.id
    
    # دستورات ادمین
    if not bot_manager.is_admin(user_id):
        await query.edit_message_text("❌ دسترسی ممنوع!")
        return
    
    if data.startswith('view_'):
        category_id = data[5:]
        await send_category_files(query.message, context, category_id)
    
    elif data.startswith('add_'):
        category_id = data[4:]
        bot_manager.pending_uploads[user_id] = {
            'category_id': category_id,
            'files': []
        }
        await query.edit_message_text(
            "📤 فایل‌ها را ارسال کنید.\n"
            "برای پایان: /finish_upload\n"
            "برای لغو: /cancel")
    
    elif data.startswith('timer_'):
        category_id = data[6:]
        bot_manager.pending_timers[user_id] = category_id
        await query.edit_message_text(
            "⏱ لطفا زمان تایمر را به ثانیه وارد کنید (0 برای غیرفعال کردن):\n"
            f"تایمر فعلی: {bot_manager.storage.get_category_timer(category_id)} ثانیه\n"
            f"تایمر جهانی: {bot_manager.storage.global_timer} ثانیه")
        return WAITING_CATEGORY_TIMER
    
    elif data.startswith('delcat_'):
        category_id = data[7:]
        if await bot_manager.storage.delete_category(category_id):
            await query.edit_message_text("✅ دسته با موفقیت حذف شد!")
        else:
            await query.edit_message_text("❌ خطا در حذف دسته!")

async def handle_category_timer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """پردازش تایمر اختصاصی دسته"""
    user_id = update.effective_user.id
    if user_id not in bot_manager.pending_timers:
        return ConversationHandler.END
    
    category_id = bot_manager.pending_timers[user_id]
    text = update.message.text.strip()
    
    try:
        seconds = int(text)
        if seconds < 0:
            seconds = 0
        
        await bot_manager.storage.save_category_timer(category_id, seconds)
        category = await bot_manager.storage.get_category(category_id)
        
        await update.message.reply_text(
            f"✅ تایمر دسته '{category['name']}' بر روی {seconds} ثانیه تنظیم شد.\n"
            f"این تنظیم فقط برای این دسته اعمال می‌شود.")
        
        del bot_manager.pending_timers[user_id]
        return ConversationHandler.END
    except ValueError:
        await update.message.reply_text("❌ لطفا یک عدد صحیح وارد کنید.")
        return WAITING_CATEGORY_TIMER

# ========================
# === UTILITY HANDLERS ===
# ========================

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """لغو عملیات جاری"""
    user_id = update.effective_user.id
    if user_id in bot_manager.pending_uploads:
        del bot_manager.pending_uploads[user_id]
    if user_id in bot_manager.pending_channels:
        del bot_manager.pending_channels[user_id]
    if user_id in bot_manager.pending_timers:
        del bot_manager.pending_timers[user_id]
    
    # لغو هرگونه وظیفه حذف در حال انتظار
    if user_id in bot_manager.delete_tasks:
        bot_manager.delete_tasks[user_id].cancel()
        del bot_manager.delete_tasks[user_id]
    
    await update.message.reply_text("❌ عملیات لغو شد.")
    return ConversationHandler.END

# ========================
# === WEB SERVER SETUP ===
# ========================

async def health_check(request):
    """صفحه سلامت برای بررسی وضعیت ربات"""
    return web.Response(text="🤖 Telegram Bot is Running!")

async def keep_alive():
    """نسخه اصلاح شده تابع keep_alive"""
    while True:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get("improved-robot-production.up.railway.app") as resp:
                    if resp.status == 200:
                        logger.info("✅ Keep-alive ping sent successfully")
                    else:
                        logger.warning(f"⚠️ Keep-alive failed: {resp.status}")
        except Exception as e:
            logger.warning(f"⚠️ Keep-alive exception: {e}")
        
        await asyncio.sleep(300)  # هر 5 دقیقه

async def run_web_server():
    """اجرای سرور وب ساده"""
    app = web.Application()
    app.router.add_get('/health', health_check)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', 10000)
    await site.start()
    logger.info("Web server started at port 10000")
    
    # اجرای نامحدود
    while True:
        await asyncio.sleep(3600)

# ========================
# ==== BOT SETUP =========
# ========================

async def run_telegram_bot():
    """اجرای اصلی ربات تلگرام - نسخه اصلاح شده"""
    application = Application.builder().token(BOT_TOKEN).build()
    
    # دریافت یوزرنیم ربات
    bot = await application.bot.get_me()
    bot_username = bot.username
    logger.info(f"Bot username: @{bot_username}")
    await bot_manager.init(bot_username, application.bot)
    
    # دستورات اصلی
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("new_category", new_category))
    application.add_handler(CommandHandler("categories", categories_list))
    application.add_handler(CommandHandler("timer", set_timer_command))
    
    # آپلود فایل‌ها
    upload_handler = ConversationHandler(
        entry_points=[CommandHandler("upload", upload_command)],
        states={
            UPLOADING: [
                MessageHandler(
                    filters.Document.ALL | filters.PHOTO | filters.VIDEO | filters.AUDIO,
                    handle_file
                )
            ]
        },
        fallbacks=[
            CommandHandler("finish_upload", finish_upload),
            CommandHandler("cancel", cancel)
        ]
    )
    application.add_handler(upload_handler)
    
    # مدیریت تایمرهای اختصاصی
    timer_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(button_handler, pattern=r"^timer_.*")],
        states={
            WAITING_CATEGORY_TIMER: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_category_timer)
            ]
        },
        fallbacks=[CommandHandler("cancel", cancel)]
    )
    application.add_handler(timer_handler)
    
    # دکمه‌های اینلاین
    application.add_handler(CallbackQueryHandler(button_handler))
    
    # اجرای ربات
    logger.info("Starting Telegram bot...")
    await application.initialize()
    await application.start()
    
    # نگه داشتن ربات در حالت اجرا
    async with application:
        await application.updater.start_polling()
        while True:
            await asyncio.sleep(3600)

async def main():
    """تابع اصلی اجرا - نسخه اصلاح شده"""
    # اجرای همزمان سرور وب و ربات تلگرام
    await asyncio.gather(
        run_web_server(),
        run_telegram_bot()
    )

if __name__ == '__main__':
    # ایجاد یک event loop جدید
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    
    try:
        # اجرای همزمان keep-alive و main
        loop.run_until_complete(asyncio.gather(
            keep_alive(),
            main()
        ))
    except KeyboardInterrupt:
        logger.info("Bot stopped by user")
    except Exception as e:
        logger.exception(f"Critical error: {e}")
    finally:
        loop.close()
