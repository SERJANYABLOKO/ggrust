import os
import asyncio
import logging
from flask import Flask
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
from steam.client import SteamClient
from steam.enums import EResult
import getpass
import time

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Хранилище сессий пользователей
user_sessions = {}
# Хранилище ожидающих логинов
pending_logins = {}

class SteamAuthManager:
    def __init__(self, user_id: int):
        self.user_id = user_id
        self.client = SteamClient()
        self.login_complete = asyncio.Event()
        self.login_result = None
        self.setup_handlers()
    
    def setup_handlers(self):
        @self.client.on('logged_on')
        def handle_logged_on():
            logger.info(f"User {self.user_id} logged into Steam")
            self.login_result = {'success': True, 'message': 'Вход выполнен успешно'}
            self.login_complete.set()
        
        @self.client.on('disconnected')
        def handle_disconnected():
            logger.info(f"User {self.user_id} disconnected from Steam")
            if not self.login_complete.is_set():
                self.login_result = {'success': False, 'message': 'Соединение разорвано'}
                self.login_complete.set()
    
    async def login_with_credentials(self, username: str, password: str, twofa_code: str = None):
        """Вход с использованием логина и пароля"""
        self.login_complete.clear()
        self.login_result = None
        
        # Запускаем процесс входа в отдельном потоке
        loop = asyncio.get_event_loop()
        
        def do_login():
            try:
                # Пытаемся войти
                result = self.client.login(
                    username=username,
                    password=password,
                    two_factor_code=twofa_code
                )
                
                if result == EResult.OK:
                    logger.info(f"Login successful for {username}")
                    return {'success': True, 'message': 'Вход выполнен успешно'}
                elif result == EResult.InvalidPassword:
                    return {'success': False, 'message': 'Неверный логин или пароль'}
                elif result == EResult.AccountLogonDenied:
                    return {'success': False, 'needs_2fa': True, 'message': 'Требуется код Steam Guard'}
                elif result == EResult.TwoFactorCodeMismatch:
                    return {'success': False, 'message': 'Неверный код двухфакторной аутентификации'}
                elif result == EResult.ServiceUnavailable:
                    return {'success': False, 'message': 'Сервис недоступен. Попробуйте позже'}
                else:
                    return {'success': False, 'message': f'Ошибка входа: {result}'}
            except Exception as e:
                logger.error(f"Login error: {e}")
                return {'success': False, 'message': f'Ошибка: {str(e)}'}
        
        # Запускаем вход в потоке
        result = await loop.run_in_executor(None, do_login)
        
        if result['success']:
            # Ждем подтверждения входа
            try:
                await asyncio.wait_for(self.login_complete.wait(), timeout=10)
                return self.login_result or result
            except asyncio.TimeoutError:
                return result
        else:
            return result
    
    def get_user_info(self):
        """Возвращает информацию о пользователе"""
        if self.client.logged_on:
            try:
                return {
                    'name': self.client.user.name,
                    'id': str(self.client.steam_id),
                    'wallet': getattr(self.client, 'wallet', 0),
                    'country': getattr(self.client, 'country', 'Не указана'),
                    'games': len(getattr(self.client, 'games', []))
                }
            except:
                return None
        return None
    
    async def logout(self):
        """Выход из Steam"""
        try:
            self.client.logout()
            return True
        except:
            return False

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
2. Введите логин Steam
3. Введите пароль
4. Если включена двухфакторная аутентификация - введите код из приложения Steam
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
• Введите логин от аккаунта Steam
• Введите пароль
• Если у вас включена двухфакторная аутентификация (Steam Guard), 
  введите код из мобильного приложения Steam

<b>📱 Требования</b>
• Действительный аккаунт Steam
• При включенном Steam Guard - доступ к приложению Steam Guard

<b>⚙️ Устранение проблем</b>
• Проверьте правильность логина и пароля
• Убедитесь, что капча не требуется (если требуется - войдите через браузер)
• При ошибке "Service Unavailable" подождите несколько минут
• Сессия сохраняется до команды /logout

<b>🔒 Безопасность</b>
• Бот не хранит ваши пароли
• Используйте выход после завершения работы
• Никогда не передавайте свои данные третьим лицам
"""
    await update.message.reply_text(help_text, parse_mode='HTML')

async def login(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /login - вход в Steam"""
    user_id = update.effective_user.id

    if user_id in user_sessions:
        await update.message.reply_text("❌ Вы уже выполнили вход! Используйте /logout")
        return

    # Запрашиваем логин
    await update.message.reply_text(
        "🔐 <b>Вход в Steam</b>\n\n"
        "Пожалуйста, отправьте ваш <b>логин</b> (не email, а имя пользователя Steam):\n\n"
        "<i>Пример: steam_username</i>\n\n"
        "⏳ У вас есть 60 секунд",
        parse_mode='HTML'
    )
    
    # Сохраняем состояние ожидания логина
    pending_logins[user_id] = {'step': 'username'}
    context.user_data['pending_login'] = True

async def handle_username(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка введенного логина"""
    user_id = update.effective_user.id
    username = update.message.text.strip()
    
    if user_id not in pending_logins or pending_logins[user_id]['step'] != 'username':
        return False
    
    pending_logins[user_id]['username'] = username
    pending_logins[user_id]['step'] = 'password'
    
    await update.message.reply_text(
        f"✅ Логин принят: <b>{username}</b>\n\n"
        "Теперь отправьте ваш <b>пароль</b>:\n"
        "<i>Пароль будет скрыт звездочками в целях безопасности</i>\n\n"
        "⏳ У вас есть 60 секунд",
        parse_mode='HTML'
    )
    return True

async def handle_password(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка введенного пароля"""
    user_id = update.effective_user.id
    password = update.message.text.strip()
    
    if user_id not in pending_logins or pending_logins[user_id]['step'] != 'password':
        return False
    
    pending_logins[user_id]['password'] = password
    
    # Показываем индикатор загрузки
    loading_msg = await update.message.reply_text("⏳ Выполняется вход в Steam...")
    
    # Создаем менеджер аутентификации
    auth_manager = SteamAuthManager(user_id)
    context.user_data['auth_manager'] = auth_manager
    
    # Пытаемся войти
    result = await auth_manager.login_with_credentials(
        pending_logins[user_id]['username'],
        password
    )
    
    if result.get('needs_2fa'):
        # Требуется двухфакторная аутентификация
        pending_logins[user_id]['step'] = '2fa'
        pending_logins[user_id]['auth_manager'] = auth_manager
        
        await loading_msg.delete()
        await update.message.reply_text(
            "🔐 <b>Требуется код двухфакторной аутентификации</b>\n\n"
            "Пожалуйста, откройте приложение Steam Guard на вашем телефоне\n"
            "и отправьте 5-значный код:\n\n"
            "<i>Пример: 12345</i>\n\n"
            "⏳ У вас есть 60 секунд",
            parse_mode='HTML'
        )
    elif result['success']:
        await loading_msg.delete()
        user_sessions[user_id] = auth_manager
        user_info = auth_manager.get_user_info()
        
        # Удаляем данные из ожидания
        del pending_logins[user_id]
        
        success_text = (
            f"✅ <b>Вход выполнен успешно!</b>\n\n"
            f"👤 <b>Имя пользователя:</b> {user_info['name']}\n"
            f"🆔 <b>Steam ID:</b> {user_info['id']}\n"
            f"🌍 <b>Страна:</b> {user_info.get('country', 'Не указана')}\n"
            f"💰 <b>Баланс кошелька:</b> {user_info.get('wallet', 0)} $\n"
            f"🎮 <b>Количество игр:</b> {user_info.get('games', 0)}\n\n"
            f"Используйте /profile для просмотра информации\n"
            f"Используйте /logout для выхода"
        )
        await update.message.reply_text(success_text, parse_mode='HTML')
    else:
        await loading_msg.delete()
        await update.message.reply_text(
            f"❌ <b>Ошибка входа</b>\n\n{result['message']}\n\n"
            f"Попробуйте снова с помощью /login",
            parse_mode='HTML'
        )
        del pending_logins[user_id]
    
    return True

async def handle_2fa(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка кода двухфакторной аутентификации"""
    user_id = update.effective_user.id
    twofa_code = update.message.text.strip()
    
    if user_id not in pending_logins or pending_logins[user_id]['step'] != '2fa':
        return False
    
    if not twofa_code.isdigit() or len(twofa_code) != 5:
        await update.message.reply_text(
            "❌ Неверный формат кода. Код должен состоять из 5 цифр.\n"
            "Попробуйте снова или используйте /login для начала заново"
        )
        return True
    
    loading_msg = await update.message.reply_text("⏳ Проверка кода подтверждения...")
    
    auth_manager = pending_logins[user_id]['auth_manager']
    result = await auth_manager.login_with_credentials(
        pending_logins[user_id]['username'],
        pending_logins[user_id]['password'],
        twofa_code
    )
    
    await loading_msg.delete()
    
    if result['success']:
        user_sessions[user_id] = auth_manager
        user_info = auth_manager.get_user_info()
        
        success_text = (
            f"✅ <b>Вход выполнен успешно!</b>\n\n"
            f"👤 <b>Имя пользователя:</b> {user_info['name']}\n"
            f"🆔 <b>Steam ID:</b> {user_info['id']}\n"
            f"🌍 <b>Страна:</b> {user_info.get('country', 'Не указана')}\n"
            f"💰 <b>Баланс кошелька:</b> {user_info.get('wallet', 0)} $\n"
            f"🎮 <b>Количество игр:</b> {user_info.get('games', 0)}\n\n"
            f"Используйте /profile для просмотра информации\n"
            f"Используйте /logout для выхода"
        )
        await update.message.reply_text(success_text, parse_mode='HTML')
        del pending_logins[user_id]
    else:
        await update.message.reply_text(
            f"❌ <b>Ошибка входа</b>\n\n{result['message']}\n\n"
            f"Попробуйте снова с помощью /login",
            parse_mode='HTML'
        )
        del pending_logins[user_id]
    
    return True

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
    success = await auth_manager.logout()
    
    if success:
        del user_sessions[user_id]
        await update.message.reply_text(
            "✅ <b>Вы успешно вышли из аккаунта Steam</b>\n\n"
            "Для повторного входа используйте /login",
            parse_mode='HTML'
        )
    else:
        await update.message.reply_text("❌ Ошибка при выходе из аккаунта")

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
        f"🔹 <b>Страна:</b> {user_info.get('country', 'Не указана')}\n"
        f"🔹 <b>Баланс:</b> {user_info.get('wallet', 0)} $\n"
        f"🔹 <b>Количество игр:</b> {user_info.get('games', 0)}\n\n"
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

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик текстовых сообщений для пошагового входа"""
    user_id = update.effective_user.id
    
    # Проверяем, ожидает ли пользователь ввода данных
    if user_id in pending_logins:
        step = pending_logins[user_id]['step']
        
        if step == 'username':
            await handle_username(update, context)
        elif step == 'password':
            await handle_password(update, context)
        elif step == '2fa':
            await handle_2fa(update, context)
        return
    
    # Если не ожидает ввода, отправляем подсказку
    await update.message.reply_text(
        "Используйте команды:\n"
        "/login - войти в Steam\n"
        "/profile - информация профиля\n"
        "/logout - выйти\n"
        "/help - помощь"
    )

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик ошибок"""
    logger.error(f"Ошибка: {context.error}")
    
    error_message = "❌ Произошла ошибка. Пожалуйста, попробуйте позже."
    
    if update and update.effective_message:
        await update.effective_message.reply_text(error_message)

def main():
    """Запуск бота"""
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
    application.add_handler(CommandHandler("cancel", lambda u,c: cancel_login(u,c)))
    
    # Обработчик текстовых сообщений (должен быть после команд)
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    # Добавляем обработчик ошибок
    application.add_error_handler(error_handler)
    
    # Запускаем бота
    logger.info("Бот запущен и готов к работе")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

async def cancel_login(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отмена процесса входа"""
    user_id = update.effective_user.id
    if user_id in pending_logins:
        del pending_logins[user_id]
        await update.message.reply_text("❌ Процесс входа отменен")
    else:
        await update.message.reply_text("Нет активного процесса входа")

# Добавляем импорт для MessageHandler и filters
from telegram.ext import MessageHandler, filters

if __name__ == '__main__':
    main()
