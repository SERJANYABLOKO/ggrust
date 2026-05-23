import os
import asyncio
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes, MessageHandler, filters
from steam.client import SteamClient
from steam.enums import EResult
import traceback

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Хранилище сессий пользователей
user_sessions = {}
pending_logins = {}

class SteamAuthManager:
    def __init__(self, user_id: int):
        self.user_id = user_id
        self.client = SteamClient()
        self.login_event = asyncio.Event()
        self.login_success = False
        self.error_message = None
        self.setup_handlers()
    
    def setup_handlers(self):
        @self.client.on('logged_on')
        def handle_logged_on():
            logger.info(f"User {self.user_id} successfully logged into Steam")
            self.login_success = True
            self.login_event.set()
        
        @self.client.on('login_error')
        def handle_login_error(result):
            logger.error(f"Login error for user {self.user_id}: {result}")
            self.login_success = False
            self.error_message = str(result)
            self.login_event.set()
        
        @self.client.on('disconnected')
        def handle_disconnected():
            logger.info(f"User {self.user_id} disconnected from Steam")
    
    async def login(self, username: str, password: str, twofa_code: str = None):
        """Выполняет вход в Steam"""
        self.login_event.clear()
        self.login_success = False
        self.error_message = None
        
        try:
            # Запускаем подключение к Steam в отдельном потоке
            loop = asyncio.get_event_loop()
            
            def do_login():
                try:
                    # Правильный метод для установки логина (или передаем прямо в login)
                    # Пытаемся войти напрямую
                    if twofa_code:
                        result = self.client.login(
                            username=username,
                            password=password,
                            two_factor_code=twofa_code
                        )
                    else:
                        result = self.client.login(
                            username=username,
                            password=password
                        )
                    
                    if result == EResult.OK:
                        logger.info(f"Login API call successful for {username}")
                        return {'success': True, 'message': 'OK'}
                    elif result == EResult.InvalidPassword:
                        return {'success': False, 'message': 'Неверный логин или пароль'}
                    elif result == EResult.AccountLogonDenied:
                        return {'success': False, 'needs_2fa': True, 'message': 'Требуется код Steam Guard'}
                    elif result == EResult.TwoFactorCodeMismatch:
                        return {'success': False, 'message': 'Неверный код двухфакторной аутентификации'}
                    elif result == EResult.ServiceUnavailable:
                        return {'success': False, 'message': 'Сервис Steam временно недоступен. Попробуйте позже'}
                    elif result == EResult.RateLimitExceeded:
                        return {'success': False, 'message': 'Слишком много попыток входа. Подождите 5-10 минут'}
                    elif result == EResult.TryAnotherCM:
                        return {'success': False, 'message': 'Попробуйте другой сервер подключения'}
                    else:
                        return {'success': False, 'message': f'Ошибка: {result}'}
                        
                except Exception as e:
                    logger.error(f"Login exception: {e}\n{traceback.format_exc()}")
                    return {'success': False, 'message': f'Ошибка: {str(e)}'}
            
            # Выполняем вход в потоке
            result = await loop.run_in_executor(None, do_login)
            
            if result['success']:
                # Ждем события logged_on
                try:
                    await asyncio.wait_for(self.login_event.wait(), timeout=15)
                    if self.login_success:
                        return {'success': True, 'message': 'Вход выполнен успешно'}
                    else:
                        return {'success': False, 'message': self.error_message or 'Ошибка при входе'}
                except asyncio.TimeoutError:
                    return {'success': False, 'message': 'Превышено время ожидания входа'}
            elif result.get('needs_2fa'):
                return result
            else:
                return result
                
        except Exception as e:
            logger.error(f"Login error: {e}\n{traceback.format_exc()}")
            return {'success': False, 'message': f'Критическая ошибка: {str(e)}'}
    
    def get_user_info(self):
        """Возвращает информацию о пользователе"""
        try:
            if self.client.logged_on:
                user_info = {
                    'name': getattr(self.client.user, 'name', 'Unknown'),
                    'id': str(self.client.steam_id) if self.client.steam_id else 'Unknown',
                    'logged_on': True
                }
                return user_info
        except Exception as e:
            logger.error(f"Error getting user info: {e}")
        return None
    
    async def logout(self):
        """Выход из Steam"""
        try:
            if self.client.connected:
                self.client.logout()
                await asyncio.sleep(1)
            return True
        except Exception as e:
            logger.error(f"Logout error: {e}")
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
/cancel - Отменить вход
/help - Помощь

<b>Как войти:</b>
1. Нажмите /login
2. Введите логин Steam (НЕ EMAIL, а имя пользователя)
3. Введите пароль
4. Если включен Steam Guard - введите код из приложения
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
• Введите логин от аккаунта Steam (НЕ email)
• Введите пароль
• Если у вас включен Steam Guard, введите код из мобильного приложения

<b>⚠️ Возможные проблемы:</b>
• <b>"InvalidPassword"</b> - неверный логин или пароль
• <b>"AccountLogonDenied"</b> - требуется код Steam Guard
• <b>"ServiceUnavailable"</b> - серверы Steam перегружены, подождите
• <b>"RateLimitExceeded"</b> - слишком много попыток, подождите 5-10 минут

<b>💡 Решения:</b>
1. Войдите в Steam через браузер для снятия блокировки
2. Убедитесь, что используете имя пользователя, а не email
3. Если не работает, подождите 10-15 минут
"""
    await update.message.reply_text(help_text, parse_mode='HTML')

async def login(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Начало процесса входа"""
    user_id = update.effective_user.id

    if user_id in user_sessions:
        await update.message.reply_text("❌ Вы уже выполнили вход! Используйте /logout")
        return

    if user_id in pending_logins:
        await update.message.reply_text("⚠️ Процесс входа уже запущен! Введите данные или используйте /cancel")
        return

    pending_logins[user_id] = {'step': 'username'}
    
    await update.message.reply_text(
        "🔐 <b>Вход в Steam</b>\n\n"
        "Пожалуйста, отправьте ваш <b>логин</b> (имя пользователя Steam):\n\n"
        "<i>Пример: your_steam_username</i>\n\n"
        "⚠️ <b>Важно:</b> Это НЕ email, а именно имя учетной записи\n\n"
        "Для отмены используйте /cancel",
        parse_mode='HTML'
    )

async def process_username(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка введенного логина"""
    user_id = update.effective_user.id
    username = update.message.text.strip()
    
    if user_id not in pending_logins or pending_logins[user_id]['step'] != 'username':
        return False
    
    if len(username) < 3:
        await update.message.reply_text(
            "❌ Слишком короткий логин. Логин должен содержать минимум 3 символа.\n"
            "Попробуйте снова или используйте /cancel"
        )
        return True
    
    pending_logins[user_id]['username'] = username
    pending_logins[user_id]['step'] = 'password'
    
    await update.message.reply_text(
        f"✅ Логин сохранен: <b>{username}</b>\n\n"
        "Теперь отправьте ваш <b>пароль</b>:\n"
        "Для отмены используйте /cancel",
        parse_mode='HTML'
    )
    return True

async def process_password(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка введенного пароля и попытка входа"""
    user_id = update.effective_user.id
    password = update.message.text.strip()
    
    if user_id not in pending_logins or pending_logins[user_id]['step'] != 'password':
        return False
    
    if len(password) < 3:
        await update.message.reply_text(
            "❌ Слишком короткий пароль.\n"
            "Попробуйте снова или используйте /cancel"
        )
        return True
    
    username = pending_logins[user_id]['username']
    
    status_msg = await update.message.reply_text(
        f"⏳ Выполняется вход в Steam для <b>{username}</b>...\n"
        f"Это может занять до 30 секунд",
        parse_mode='HTML'
    )
    
    auth_manager = SteamAuthManager(user_id)
    context.user_data['auth_manager'] = auth_manager
    
    result = await auth_manager.login(username, password)
    
    if result.get('needs_2fa'):
        pending_logins[user_id]['step'] = '2fa'
        pending_logins[user_id]['auth_manager'] = auth_manager
        pending_logins[user_id]['password'] = password
        
        await status_msg.delete()
        await update.message.reply_text(
            "🔐 <b>Требуется код двухфакторной аутентификации</b>\n\n"
            "Откройте приложение Steam Guard на телефоне и отправьте 5-значный код:\n\n"
            "<i>Пример: 12345</i>\n\n"
            "Для отмены используйте /cancel",
            parse_mode='HTML'
        )
    elif result['success']:
        await status_msg.delete()
        user_sessions[user_id] = auth_manager
        user_info = auth_manager.get_user_info()
        
        del pending_logins[user_id]
        
        success_text = (
            f"✅ <b>Вход выполнен успешно!</b>\n\n"
            f"👤 <b>Имя пользователя:</b> {user_info['name'] if user_info else username}\n"
            f"🆔 <b>Steam ID:</b> {user_info['id'] if user_info else 'Неизвестно'}\n\n"
            f"Используйте /profile для просмотра информации\n"
            f"Используйте /logout для выхода"
        )
        await update.message.reply_text(success_text, parse_mode='HTML')
    else:
        await status_msg.delete()
        await update.message.reply_text(
            f"❌ <b>Ошибка входа</b>\n\n{result['message']}\n\n"
            f"💡 <b>Советы:</b>\n"
            f"• Проверьте правильность логина и пароля\n"
            f"• Используйте имя пользователя, а не email\n"
            f"• Подождите несколько минут и попробуйте снова\n\n"
            f"Для повторной попытки используйте /login",
            parse_mode='HTML'
        )
        del pending_logins[user_id]
    
    return True

async def process_2fa(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка кода двухфакторной аутентификации"""
    user_id = update.effective_user.id
    twofa_code = update.message.text.strip()
    
    if user_id not in pending_logins or pending_logins[user_id]['step'] != '2fa':
        return False
    
    if not twofa_code.isdigit() or len(twofa_code) != 5:
        await update.message.reply_text(
            "❌ Неверный формат кода. Код должен состоять из 5 цифр.\n"
            "Попробуйте снова или используйте /cancel"
        )
        return True
    
    status_msg = await update.message.reply_text("⏳ Проверка кода подтверждения...")
    
    auth_manager = pending_logins[user_id]['auth_manager']
    username = pending_logins[user_id]['username']
    password = pending_logins[user_id]['password']
    
    result = await auth_manager.login(username, password, twofa_code)
    
    await status_msg.delete()
    
    if result['success']:
        user_sessions[user_id] = auth_manager
        user_info = auth_manager.get_user_info()
        
        success_text = (
            f"✅ <b>Вход выполнен успешно!</b>\n\n"
            f"👤 <b>Имя пользователя:</b> {user_info['name'] if user_info else username}\n"
            f"🆔 <b>Steam ID:</b> {user_info['id'] if user_info else 'Неизвестно'}\n\n"
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
    """Выход из Steam"""
    user_id = update.effective_user.id
    
    if user_id not in user_sessions:
        await update.message.reply_text("❌ Вы не авторизованы в Steam")
        return
    
    status_msg = await update.message.reply_text("⏳ Выполняется выход...")
    
    auth_manager = user_sessions[user_id]
    success = await auth_manager.logout()
    
    await status_msg.delete()
    
    if success:
        del user_sessions[user_id]
        await update.message.reply_text("✅ <b>Вы вышли из аккаунта Steam</b>\n\n/login - для входа", parse_mode='HTML')
    else:
        await update.message.reply_text("❌ Ошибка при выходе")

async def profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Информация профиля"""
    user_id = update.effective_user.id
    
    if user_id not in user_sessions:
        await update.message.reply_text("❌ Вы не авторизованы в Steam. Используйте /login")
        return
    
    auth_manager = user_sessions[user_id]
    user_info = auth_manager.get_user_info()
    
    if not user_info:
        await update.message.reply_text("❌ Не удалось получить информацию профиля")
        return
    
    profile_text = (
        f"👤 <b>Профиль Steam</b>\n\n"
        f"🔹 <b>Имя:</b> {user_info.get('name', 'Неизвестно')}\n"
        f"🔹 <b>Steam ID:</b> {user_info.get('id', 'Неизвестно')}\n\n"
        f"✅ <b>Статус:</b> Активен"
    )
    
    await update.message.reply_text(profile_text, parse_mode='HTML')

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отмена процесса входа"""
    user_id = update.effective_user.id
    
    if user_id in pending_logins:
        del pending_logins[user_id]
        await update.message.reply_text("❌ Процесс входа отменен\n\nДля новой попытки используйте /login")
    else:
        await update.message.reply_text("Нет активного процесса входа")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка всех текстовых сообщений"""
    user_id = update.effective_user.id
    
    if user_id in pending_logins:
        step = pending_logins[user_id]['step']
        
        if step == 'username':
            await process_username(update, context)
        elif step == 'password':
            await process_password(update, context)
        elif step == '2fa':
            await process_2fa(update, context)
    else:
        await update.message.reply_text(
            "🤖 <b>Доступные команды:</b>\n\n"
            "/login - Войти в Steam\n"
            "/profile - Информация профиля\n"
            "/logout - Выйти\n"
            "/help - Помощь",
            parse_mode='HTML'
        )

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка callback-запросов"""
    query = update.callback_query
    await query.answer()
    
    if query.data == "login_steam":
        await login(update, context)
    elif query.data == "help":
        await help_command(update, context)

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Глобальный обработчик ошибок"""
    logger.error(f"Exception: {context.error}")

def main():
    """Запуск бота"""
    token = os.getenv('TELEGRAM_BOT_TOKEN')
    
    if not token:
        logger.error("TELEGRAM_BOT_TOKEN не найден")
        print("ОШИБКА: Установите TELEGRAM_BOT_TOKEN")
        return
    
    # Сбрасываем вебхук перед запуском
    import requests
    try:
        requests.get(f"https://api.telegram.org/bot{token}/deleteWebhook")
        logger.info("Webhook deleted")
    except:
        pass
    
    application = Application.builder().token(token).build()
    
    # Добавляем обработчики
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("login", login))
    application.add_handler(CommandHandler("logout", logout))
    application.add_handler(CommandHandler("profile", profile))
    application.add_handler(CommandHandler("cancel", cancel))
    application.add_handler(CallbackQueryHandler(handle_callback))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    application.add_error_handler(error_handler)
    
    logger.info("🚀 Бот запущен")
    print("✅ Бот успешно запущен!")
    
    # Запускаем с очисткой старых обновлений
    application.run_polling(drop_pending_updates=True)

if __name__ == '__main__':
    main()
