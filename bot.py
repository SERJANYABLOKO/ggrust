import os
import asyncio
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
from steam import SteamClient
import qrcode
from io import BytesIO
from dotenv import load_dotenv

# Загрузка переменных окружения
load_dotenv()

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Хранилище сессий пользователей (в реальном проекте используйте БД)
user_sessions = {}

class SteamAuthManager:
    def __init__(self, user_id: int):
        self.user_id = user_id
        self.client = SteamClient()
        self.login_complete = asyncio.Event()
        self.qr_generated = False
        self.qr_data = None
        self.setup_handlers()
    
    def setup_handlers(self):
        @self.client.on('logged_on')
        def handle_logged_on():
            logger.info(f"User {self.user_id} logged into Steam")
            self.login_complete.set()
        
        @self.client.on('disconnected')
        def handle_disconnected():
            logger.info(f"User {self.user_id} disconnected from Steam")
            self.login_complete.clear()
        
        @self.client.on('qrcode')
        def handle_qrcode(qr_data):
            self.qr_data = qr_data
            self.qr_generated = True
    
    async def start_login(self):
        """Начинает процесс входа через QR-код"""
        self.login_complete.clear()
        self.qr_generated = False
        self.qr_data = None
        
        # Запускаем подключение к Steam
        self.client.cli_login()
        
        # Ждем генерации QR-кода
        for _ in range(30):  # Максимум 30 секунд ожидания
            if self.qr_generated:
                break
            await asyncio.sleep(1)
        
        if not self.qr_generated:
            return None, "Не удалось сгенерировать QR-код"
        
        return self.qr_data, "Отсканируйте QR-код через мобильное приложение Steam"
    
    async def check_login_status(self):
        """Проверяет статус входа"""
        try:
            await asyncio.wait_for(self.login_complete.wait(), timeout=120)  # 2 минуты на вход
            return True, self.client.user.name if self.client.user else "Аккаунт"
        except asyncio.TimeoutError:
            return False, "Время ожидания входа истекло"
    
    def get_user_info(self):
        """Возвращает информацию о пользователе"""
        if self.client.logged_on:
            return {
                'name': self.client.user.name,
                'id': self.client.steam_id,
                'wallet': self.client.wallet,
                'country': self.client.country
            }
        return None
    
    async def logout(self):
        """Выход из Steam"""
        self.client.logout()

# Команды бота
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /start"""
    welcome_text = """
🎮 <b>Steam Auth Bot</b>

Добро пожаловать! Этот бот поможет вам войти в аккаунт Steam.

<b>Доступные команды:</b>
/start - Показать это сообщение
/login - Войти через Steam
/logout - Выйти из аккаунта
/profile - Показать информацию профиля
/help - Помощь

<b>Как войти:</b>
1. Нажмите /login
2. Отсканируйте QR-код приложением Steam
3. Подтвердите вход в мобильном приложении
    """
    
    keyboard = [
        [InlineKeyboardButton("🔑 Войти через Steam", callback_data="login_steam")],
        [InlineKeyboardButton("❓ Помощь", callback_data="help")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        welcome_text,
        reply_markup=reply_markup,
        parse_mode='HTML'
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /help"""
    help_text = """
📚 <b>Помощь по использованию бота</b>

<b>🔐 Вход через Steam</b>
• Используйте команду /login
• Отсканируйте QR-код в приложении Steam
• Перейдите в Steam → Настройки → Авторизация по QR
• Подтвердите вход на телефоне

<b>📱 Требования</b>
• Установленное приложение Steam на телефоне
• Активная сессия в мобильном Steam

<b>⚙️ Устранение проблем</b>
• Если QR-код не отображается - попробуйте снова через минуту
• Если истекло время - используйте /login повторно
• Сессия сохраняется до команды /logout

<b>🔒 Безопасность</b>
• Никогда не передавайте QR-код третьим лицам
• Используйте выход после завершения работы
"""
    await update.message.reply_text(help_text, parse_mode='HTML')

async def login(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /login - вход через Steam"""
    user_id = update.effective_user.id
    
    # Проверяем, не выполнен ли уже вход
    if user_id in user_sessions:
        await update.message.reply_text(
            "❌ Вы уже выполнили вход!\n"
            "Используйте /logout для выхода из текущего аккаунта"
        )
        return
    
    await update.message.reply_text(
        "🔐 <b>Подготовка к входу в Steam...</b>\n\n"
        "Пожалуйста, подождите, генерирую QR-код...",
        parse_mode='HTML'
    )
    
    # Создаем менеджер авторизации
    auth_manager = SteamAuthManager(user_id)
    context.user_data['auth_manager'] = auth_manager
    
    # Начинаем процесс входа
    qr_data, message = await auth_manager.start_login()
    
    if not qr_data:
        await update.message.reply_text(f"❌ Ошибка: {message}")
        return
    
    # Создаем QR-код
    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_L,
        box_size=10,
        border=4,
    )
    qr.add_data(qr_data)
    qr.make(fit=True)
    
    img = qr.make_image(fill_color="black", back_color="white")
    
    # Сохраняем в буфер
    bio = BytesIO()
    bio.name = 'qrcode.png'
    img.save(bio, 'PNG')
    bio.seek(0)
    
    # Отправляем QR-код
    await update.message.reply_photo(
        photo=bio,
        caption=f"✅ {message}\n\n"
                "📱 <b>Инструкция:</b>\n"
                "1. Откройте приложение Steam на телефоне\n"
                "2. Перейдите в раздел 'Настройки'\n"
                "3. Выберите 'Авторизация по QR-коду'\n"
                "4. Отсканируйте QR-код камерой телефона\n\n"
                "⏳ QR-код действителен 2 минуты",
        parse_mode='HTML'
    )
    
    # Ожидаем подтверждения входа
    await update.message.reply_text(
        "⏳ <b>Ожидание подтверждения входа...</b>\n"
        "После сканирования QR-кода подтвердите вход в приложении Steam",
        parse_mode='HTML'
    )
    
    # Проверяем статус входа
    success, result = await auth_manager.check_login_status()
    
    if success:
        user_sessions[user_id] = auth_manager
        user_info = auth_manager.get_user_info()
        
        success_text = (
            f"✅ <b>Вход выполнен успешно!</b>\n\n"
            f"👤 <b>Имя пользователя:</b> {user_info['name']}\n"
            f"🆔 <b>Steam ID:</b> {user_info['id']}\n"
            f"🌍 <b>Страна:</b> {user_info.get('country', 'Не указана')}\n\n"
            f"Используйте /profile для просмотра информации\n"
            f"Используйте /logout для выхода"
        )
        await update.message.reply_text(success_text, parse_mode='HTML')
    else:
        await update.message.reply_text(f"❌ Ошибка входа: {result}")

async def logout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /logout - выход из Steam"""
    user_id = update.effective_user.id
    
    if user_id not in user_sessions:
        await update.message.reply_text(
            "❌ Вы не авторизованы в Steam.\n"
            "Используйте /login для входа"
        )
        return
    
    auth_manager = user_sessions[user_id]
    await auth_manager.logout()
    del user_sessions[user_id]
    
    await update.message.reply_text(
        "✅ <b>Вы успешно вышли из аккаунта Steam</b>\n\n"
        "Для повторного входа используйте /login",
        parse_mode='HTML'
    )

async def profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /profile - информация профиля"""
    user_id = update.effective_user.id
    
    if user_id not in user_sessions:
        await update.message.reply_text(
            "❌ Вы не авторизованы в Steam.\n"
            "Используйте /login для входа"
        )
        return
    
    auth_manager = user_sessions[user_id]
    user_info = auth_manager.get_user_info()
    
    if not user_info:
        await update.message.reply_text("❌ Не удалось получить информацию профиля")
        return
    
    profile_text = (
        f"👤 <b>Профиль Steam</b>\n\n"
        f"🔹 <b>Имя:</b> {user_info['name']}\n"
        f"🔹 <b>Steam ID:</b> {user_info['id']}\n"
        f"🔹 <b>Страна:</b> {user_info.get('country', 'Не указана')}\n\n"
        f"✅ <b>Статус:</b> Активен"
    )
    
    await update.message.reply_text(profile_text, parse_mode='HTML')

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик нажатий на инлайн-кнопки"""
    query = update.callback_query
    await query.answer()
    
    if query.data == "login_steam":
        await login(update, context)
    elif query.data == "help":
        await help_command(update, context)

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик ошибок"""
    logger.error(f"Ошибка: {context.error}")
    if update and update.effective_message:
        await update.effective_message.reply_text(
            "❌ Произошла ошибка. Пожалуйста, попробуйте позже."
        )

def main():
    """Запуск бота"""
    # Получаем токен из переменных окружения
    token = os.getenv('TELEGRAM_BOT_TOKEN')
    
    if not token:
        logger.error("TELEGRAM_BOT_TOKEN не найден в переменных окружения")
        return
    
    # Создаем приложение
    application = Application.builder().token(token).build()
    
    # Добавляем обработчики команд
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("login", login))
    application.add_handler(CommandHandler("logout", logout))
    application.add_handler(CommandHandler("profile", profile))
    application.add_handler(CallbackQueryHandler(handle_callback))
    
    # Добавляем обработчик ошибок
    application.add_error_handler(error_handler)
    
    # Запускаем бота
    logger.info("Бот запущен и готов к работе")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    main()
