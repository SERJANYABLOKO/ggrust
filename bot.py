import os
import logging
import time
import requests
import sqlite3
import hashlib
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Updater, CommandHandler, CallbackQueryHandler, MessageHandler, Filters
from steam.client import SteamClient
from steam.enums import EResult

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Хранилище сессий пользователей
user_sessions = {}
pending_logins = {}

class Database:
    def __init__(self, db_path='steam_accounts.db'):
        self.db_path = db_path
        self.init_db()
    
    def init_db(self):
        """Инициализация базы данных"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        # Таблица для хранения аккаунтов
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS steam_accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER NOT NULL,
                username TEXT NOT NULL,
                password TEXT NOT NULL,
                steam_id TEXT,
                profile_name TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_login TIMESTAMP,
                login_count INTEGER DEFAULT 1,
                UNIQUE(telegram_id, username)
            )
        ''')
        
        # Таблица для логов входов
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS login_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER NOT NULL,
                username TEXT NOT NULL,
                login_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                success BOOLEAN,
                error_message TEXT,
                ip_address TEXT
            )
        ''')
        
        conn.commit()
        conn.close()
        logger.info("База данных инициализирована")
    
    def save_account(self, telegram_id, username, password, steam_id=None, profile_name=None):
        """Сохранение аккаунта в базу"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        try:
            # Проверяем, существует ли уже аккаунт
            cursor.execute('SELECT id, login_count FROM steam_accounts WHERE telegram_id = ? AND username = ?', 
                          (telegram_id, username))
            existing = cursor.fetchone()
            
            if existing:
                # Обновляем существующий аккаунт
                cursor.execute('''
                    UPDATE steam_accounts 
                    SET password = ?, steam_id = ?, profile_name = ?, 
                        last_login = CURRENT_TIMESTAMP, login_count = login_count + 1
                    WHERE telegram_id = ? AND username = ?
                ''', (password, steam_id, profile_name, telegram_id, username))
                logger.info(f"Обновлен аккаунт {username} для пользователя {telegram_id}")
            else:
                # Добавляем новый аккаунт
                cursor.execute('''
                    INSERT INTO steam_accounts (telegram_id, username, password, steam_id, profile_name)
                    VALUES (?, ?, ?, ?, ?)
                ''', (telegram_id, username, password, steam_id, profile_name))
                logger.info(f"Сохранен новый аккаунт {username} для пользователя {telegram_id}")
            
            conn.commit()
            return True
        except Exception as e:
            logger.error(f"Ошибка сохранения аккаунта: {e}")
            return False
        finally:
            conn.close()
    
    def get_accounts(self, telegram_id):
        """Получение всех аккаунтов пользователя"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        cursor.execute('''
            SELECT username, steam_id, profile_name, created_at, last_login, login_count
            FROM steam_accounts 
            WHERE telegram_id = ?
            ORDER BY last_login DESC
        ''', (telegram_id,))
        
        accounts = cursor.fetchall()
        conn.close()
        return accounts
    
    def get_account(self, telegram_id, username):
        """Получение конкретного аккаунта"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        cursor.execute('''
            SELECT username, password, steam_id, profile_name
            FROM steam_accounts 
            WHERE telegram_id = ? AND username = ?
        ''', (telegram_id, username))
        
        account = cursor.fetchone()
        conn.close()
        return account
    
    def delete_account(self, telegram_id, username):
        """Удаление аккаунта"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        cursor.execute('DELETE FROM steam_accounts WHERE telegram_id = ? AND username = ?', 
                      (telegram_id, username))
        conn.commit()
        deleted = cursor.rowcount > 0
        conn.close()
        return deleted
    
    def log_login(self, telegram_id, username, success, error_message=None, ip_address=None):
        """Логирование попыток входа"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        cursor.execute('''
            INSERT INTO login_logs (telegram_id, username, success, error_message, ip_address)
            VALUES (?, ?, ?, ?, ?)
        ''', (telegram_id, username, success, error_message, ip_address))
        
        conn.commit()
        conn.close()
    
    def get_login_stats(self, telegram_id):
        """Статистика входов пользователя"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        cursor.execute('''
            SELECT 
                COUNT(*) as total_logins,
                SUM(CASE WHEN success = 1 THEN 1 ELSE 0 END) as successful_logins,
                COUNT(DISTINCT username) as unique_accounts
            FROM login_logs 
            WHERE telegram_id = ?
        ''', (telegram_id,))
        
        stats = cursor.fetchone()
        conn.close()
        
        return {
            'total': stats[0] or 0,
            'successful': stats[1] or 0,
            'unique_accounts': stats[2] or 0
        }

# Инициализируем базу данных
db = Database()

class SteamAuthManager:
    def __init__(self, user_id: int):
        self.user_id = user_id
        self.client = SteamClient()
        self.login_success = False
        self.login_result = None
        self.setup_handlers()
    
    def setup_handlers(self):
        @self.client.on('logged_on')
        def handle_logged_on():
            logger.info(f"User {self.user_id} logged into Steam")
            self.login_success = True
            self.login_result = {'success': True, 'message': 'Вход выполнен'}
        
        @self.client.on('login_error')
        def handle_login_error(result):
            logger.error(f"Login error for user {self.user_id}: {result}")
            self.login_success = False
            self.login_result = {'success': False, 'message': str(result)}
    
    def login(self, username: str, password: str, twofa_code: str = None):
        """Синхронный вход в Steam"""
        self.login_success = False
        self.login_result = None
        
        try:
            self.client.set_connection_timeout(30)
            
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
                logger.info(f"Login OK for {username}")
                time.sleep(2)
                return {'success': True, 'message': 'Вход выполнен успешно'}
            
            elif result == EResult.InvalidPassword:
                return {'success': False, 'message': '❌ Неверный логин или пароль'}
            
            elif result == EResult.AccountLogonDenied:
                return {'success': False, 'needs_2fa': True, 'message': '🔐 Требуется код Steam Guard'}
            
            elif result == EResult.TwoFactorCodeMismatch:
                return {'success': False, 'message': '❌ Неверный код двухфакторной аутентификации'}
            
            elif result == EResult.ServiceUnavailable:
                return {'success': False, 'message': '⚠️ Сервис Steam временно недоступен. Попробуйте через 5-10 минут'}
            
            elif result == EResult.RateLimitExceeded:
                return {'success': False, 'message': '⚠️ Слишком много попыток входа! Steam заблокировал вход на 30-60 минут.\n\n💡 Решение:\n• Подождите 1 час\n• Войдите в Steam через браузер\n• Используйте VPN'}
            
            elif result == EResult.TryAnotherCM:
                return {'success': False, 'message': '🔄 Попробуйте другой сервер подключения. Подождите 5 минут'}
            
            elif result == 85:
                return {'success': False, 'message': '⚠️ Лимит попыток входа превышен! (Ошибка 85)\n\nПодождите 1 час перед следующей попыткой.\nВойдите в Steam через браузер для разблокировки.'}
            
            else:
                return {'success': False, 'message': f'❌ Ошибка {result}\n\nПопробуйте позже или войдите через браузер Steam'}
                
        except Exception as e:
            error_msg = str(e)
            logger.error(f"Login error: {error_msg}")
            
            if "Timeout" in error_msg or "timeout" in error_msg:
                return {'success': False, 'message': '⏰ Таймаут подключения. Проверьте соединение или попробуйте VPN'}
            else:
                return {'success': False, 'message': f'❌ Техническая ошибка: {error_msg[:100]}'}
    
    def get_user_info(self):
        try:
            if self.client.logged_on:
                return {
                    'name': getattr(self.client.user, 'name', 'Unknown'),
                    'id': str(self.client.steam_id) if self.client.steam_id else 'Unknown',
                }
        except:
            pass
        return None
    
    def logout(self):
        try:
            if self.client.connected:
                self.client.logout()
            return True
        except:
            return False

# Обработчики команд
def start(update: Update, context):
    welcome_text = """
🎮 <b>Steam Auth Bot</b>

Добро пожаловать! Бот поможет войти в аккаунт Steam и сохраняет все ваши аккаунты.

<b>Команды:</b>
/start - Это сообщение
/login - Войти в Steam
/logout - Выйти
/profile - Информация профиля
/myaccounts - Мои сохраненные аккаунты
/switch - Переключиться на другой аккаунт
/delaccount - Удалить сохраненный аккаунт
/stats - Статистика
/cancel - Отменить вход
/help - Помощь

<b>Как войти:</b>
1. Нажмите /login
2. Введите логин (НЕ email)
3. Введите пароль
4. Если есть Steam Guard - введите код

⚠️ <b>Важно:</b> При ошибке "RateLimitExceeded (85)" подождите 1 час
"""
    
    keyboard = [[InlineKeyboardButton("🔑 Войти", callback_data="login_steam")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    update.message.reply_text(welcome_text, reply_markup=reply_markup, parse_mode='HTML')

def help_command(update: Update, context):
    help_text = """
📚 <b>Помощь по ошибкам</b>

<b>❌ Ошибка 85 (RateLimitExceeded):</b>
• Слишком много неудачных попыток входа
• Steam временно заблокировал вход
• <b>Решение:</b> Подождите 1 час, затем войдите в Steam через браузер

<b>❌ InvalidPassword:</b>
• Неверный логин или пароль
• <b>Решение:</b> Проверьте данные, используйте имя пользователя (НЕ email)

<b>❌ AccountLogonDenied:</b>
• Требуется код Steam Guard
• <b>Решение:</b> Введите код из приложения Steam

<b>⚠️ ServiceUnavailable:</b>
• Серверы Steam перегружены
• <b>Решение:</b> Подождите 10-15 минут

<b>💡 Общие советы:</b>
1. Войдите в Steam через браузер перед использованием бота
2. Убедитесь, что аккаунт не заблокирован
3. Используйте VPN если Steam недоступен в вашем регионе
4. Не делайте много попыток подряд - ждите между ними
"""
    update.message.reply_text(help_text, parse_mode='HTML')

def myaccounts(update: Update, context):
    user_id = update.effective_user.id
    accounts = db.get_accounts(user_id)
    
    if not accounts:
        update.message.reply_text(
            "📭 <b>У вас нет сохраненных аккаунтов</b>\n\n"
            "Используйте /login чтобы добавить аккаунт",
            parse_mode='HTML'
        )
        return
    
    text = "📋 <b>Ваши сохраненные аккаунты Steam</b>\n\n"
    
    for i, account in enumerate(accounts, 1):
        username, steam_id, profile_name, created_at, last_login, login_count = account
        
        text += f"{i}. <b>{username}</b>\n"
        if profile_name:
            text += f"   👤 Имя: {profile_name}\n"
        if steam_id:
            text += f"   🆔 Steam ID: {steam_id}\n"
        text += f"   📅 Добавлен: {created_at[:10]}\n"
        if last_login:
            text += f"   🔑 Последний вход: {last_login[:10]}\n"
        text += f"   📊 Входов: {login_count}\n\n"
    
    text += "\n<b>Команды:</b>\n"
    text += "/switch <логин> - Переключиться на аккаунт\n"
    text += "/delaccount <логин> - Удалить аккаунт"
    
    update.message.reply_text(text, parse_mode='HTML')

def switch_account(update: Update, context):
    user_id = update.effective_user.id
    
    if not context.args:
        accounts = db.get_accounts(user_id)
        if not accounts:
            update.message.reply_text("❌ У вас нет сохраненных аккаунтов")
            return
        
        text = "🔄 <b>Переключение аккаунта</b>\n\n"
        text += "Используйте: /switch <логин>\n\n"
        text += "<b>Доступные аккаунты:</b>\n"
        for account in accounts:
            text += f"• {account[0]}\n"
        
        update.message.reply_text(text, parse_mode='HTML')
        return
    
    username = ' '.join(context.args)
    account = db.get_account(user_id, username)
    
    if not account:
        update.message.reply_text(f"❌ Аккаунт {username} не найден")
        return
    
    if user_id in user_sessions:
        user_sessions[user_id].logout()
        del user_sessions[user_id]
    
    # Сохраняем для автоматического входа
    context.user_data['auto_login'] = {
        'username': account[0],
        'password': account[1]
    }
    
    update.message.reply_text(
        f"🔄 <b>Переключение на {username}</b>\n\n"
        f"Используйте /login для входа с сохраненными данными",
        parse_mode='HTML'
    )

def delaccount(update: Update, context):
    user_id = update.effective_user.id
    
    if not context.args:
        accounts = db.get_accounts(user_id)
        if not accounts:
            update.message.reply_text("❌ У вас нет сохраненных аккаунтов")
            return
        
        text = "🗑 <b>Удаление аккаунта</b>\n\n"
        text += "Используйте: /delaccount <логин>\n\n"
        text += "<b>Ваши аккаунты:</b>\n"
        for account in accounts:
            text += f"• {account[0]}\n"
        
        update.message.reply_text(text, parse_mode='HTML')
        return
    
    username = ' '.join(context.args)
    
    if db.delete_account(user_id, username):
        # Если удаляем текущую сессию
        if user_id in user_sessions:
            current = user_sessions[user_id].get_user_info()
            if current and current['name'] == username:
                user_sessions[user_id].logout()
                del user_sessions[user_id]
        
        update.message.reply_text(f"✅ Аккаунт {username} удален")
    else:
        update.message.reply_text(f"❌ Аккаунт {username} не найден")

def stats(update: Update, context):
    user_id = update.effective_user.id
    stats_data = db.get_login_stats(user_id)
    accounts = db.get_accounts(user_id)
    
    text = "📊 <b>Ваша статистика</b>\n\n"
    text += f"📝 Всего аккаунтов: {len(accounts)}\n"
    text += f"🔑 Попыток входа: {stats_data['total']}\n"
    text += f"✅ Успешных входов: {stats_data['successful']}\n"
    text += f"📈 Успешность: {int(stats_data['successful']/stats_data['total']*100) if stats_data['total'] > 0 else 0}%\n"
    
    update.message.reply_text(text, parse_mode='HTML')

def login(update: Update, context):
    user_id = update.effective_user.id
    
    # Проверяем, есть ли данные для автоматического входа
    if context.user_data.get('auto_login'):
        auto = context.user_data['auto_login']
        update.message.reply_text(
            f"🔐 <b>Найден сохраненный аккаунт</b>\n\n"
            f"Аккаунт: {auto['username']}\n\n"
            f"Используйте /autologin для быстрого входа\n"
            f"Или /login для ручного ввода",
            parse_mode='HTML'
        )
        return
    
    if user_id in user_sessions:
        update.message.reply_text("❌ Вы уже вошли! Используйте /logout")
        return
    
    if user_id in pending_logins:
        update.message.reply_text("⚠️ Вход уже начат! Используйте /cancel")
        return
    
    pending_logins[user_id] = {'step': 'username', 'attempts': 0}
    
    update.message.reply_text(
        "🔐 <b>Вход в Steam</b>\n\n"
        "Отправьте ваш <b>логин</b> (имя пользователя):\n"
        "<i>Пример: your_username</i>\n\n"
        "⚠️ <b>Важно:</b>\n"
        "• Используйте имя пользователя, НЕ email\n"
        "• При ошибке 85 подождите 1 час\n\n"
        "Для отмены: /cancel",
        parse_mode='HTML'
    )

def autologin(update: Update, context):
    """Автоматический вход с сохраненными данными"""
    user_id = update.effective_user.id
    
    if not context.user_data.get('auto_login'):
        update.message.reply_text("❌ Нет сохраненных данных для автоматического входа")
        return
    
    auto = context.user_data['auto_login']
    username = auto['username']
    password = auto['password']
    
    status_msg = update.message.reply_text(
        f"⏳ Автоматический вход для <b>{username}</b>...",
        parse_mode='HTML'
    )
    
    auth_manager = SteamAuthManager(user_id)
    result = auth_manager.login(username, password)
    
    try:
        status_msg.delete()
    except:
        pass
    
    if result.get('needs_2fa'):
        pending_logins[user_id] = {
            'step': '2fa', 
            'username': username,
            'password': password,
            'auth_manager': auth_manager,
            'attempts': 0
        }
        update.message.reply_text(
            f"🔐 <b>Требуется код Steam Guard для {username}</b>\n\n"
            f"Отправьте 5-значный код из приложения Steam:\n"
            f"/cancel - отмена",
            parse_mode='HTML'
        )
    elif result['success']:
        user_sessions[user_id] = auth_manager
        user_info = auth_manager.get_user_info()
        
        # Сохраняем в базу
        db.save_account(
            user_id, 
            username, 
            password,
            user_info.get('id') if user_info else None,
            user_info.get('name') if user_info else None
        )
        db.log_login(user_id, username, True)
        
        update.message.reply_text(
            f"✅ <b>Вход выполнен!</b>\n\n"
            f"👤 {username}\n\n"
            f"/profile - информация\n"
            f"/logout - выход",
            parse_mode='HTML'
        )
    else:
        db.log_login(user_id, username, False, result['message'])
        update.message.reply_text(
            f"❌ <b>Ошибка входа</b>\n\n"
            f"{result['message']}\n\n"
            f"/login - ручной вход",
            parse_mode='HTML'
        )

def cancel(update: Update, context):
    user_id = update.effective_user.id
    
    if user_id in pending_logins:
        del pending_logins[user_id]
        update.message.reply_text("❌ Вход отменен\n\n/login - для новой попытки")
    else:
        update.message.reply_text("Нет активного входа")

def logout(update: Update, context):
    user_id = update.effective_user.id
    
    if user_id not in user_sessions:
        update.message.reply_text("❌ Вы не авторизованы")
        return
    
    auth_manager = user_sessions[user_id]
    auth_manager.logout()
    del user_sessions[user_id]
    
    # Очищаем авто-вход
    if 'auto_login' in context.user_data:
        del context.user_data['auto_login']
    
    update.message.reply_text("✅ Вы вышли из аккаунта\n\n/login - для входа", parse_mode='HTML')

def profile(update: Update, context):
    user_id = update.effective_user.id
    
    if user_id not in user_sessions:
        update.message.reply_text("❌ Вы не авторизованы. Используйте /login")
        return
    
    auth_manager = user_sessions[user_id]
    user_info = auth_manager.get_user_info()
    
    if not user_info:
        update.message.reply_text("❌ Не удалось получить информацию. Возможно сессия истекла.\nИспользуйте /login")
        return
    
    text = f"👤 <b>Профиль Steam</b>\n\n🔹 Имя: {user_info['name']}\n🔹 Steam ID: {user_info['id']}\n\n✅ Статус: Активен"
    update.message.reply_text(text, parse_mode='HTML')

def handle_message(update: Update, context):
    user_id = update.effective_user.id
    text = update.message.text.strip()
    
    if user_id in pending_logins:
        step = pending_logins[user_id]['step']
        
        if step == 'username':
            if len(text) < 3:
                update.message.reply_text("❌ Слишком короткий логин. Попробуйте снова или /cancel")
                return
            
            pending_logins[user_id]['username'] = text
            pending_logins[user_id]['step'] = 'password'
            update.message.reply_text(
                f"✅ Логин: {text}\n\n"
                f"Теперь отправьте <b>пароль</b>:\n"
                f"/cancel - отмена\n\n"
                f"⚠️ <b>Важно:</b> При ошибке 85 подождите 1 час",
                parse_mode='HTML'
            )
            
        elif step == 'password':
            if len(text) < 3:
                update.message.reply_text("❌ Слишком короткий пароль. Попробуйте снова")
                return
            
            pending_logins[user_id]['attempts'] = pending_logins[user_id].get('attempts', 0) + 1
            attempts = pending_logins[user_id]['attempts']
            
            if attempts >= 3:
                update.message.reply_text(
                    "⚠️ <b>Слишком много попыток!</b>\n\n"
                    "Steam может временно заблокировать вход.\n"
                    "Подождите 30-60 минут перед следующей попыткой.\n\n"
                    "Также рекомендуется:\n"
                    "1. Войти в Steam через браузер\n"
                    "2. Использовать VPN\n"
                    "3. Проверить правильность данных\n\n"
                    "Используйте /login через час",
                    parse_mode='HTML'
                )
                del pending_logins[user_id]
                return
            
            username = pending_logins[user_id]['username']
            password = text
            
            status_msg = update.message.reply_text(
                f"⏳ Вход в Steam для <b>{username}</b>...\n"
                f"Попытка {attempts}/3\n"
                f"Это может занять до 30 секунд",
                parse_mode='HTML'
            )
            
            auth_manager = SteamAuthManager(user_id)
            result = auth_manager.login(username, password)
            
            try:
                status_msg.delete()
            except:
                pass
            
            if result.get('needs_2fa'):
                pending_logins[user_id]['step'] = '2fa'
                pending_logins[user_id]['auth_manager'] = auth_manager
                pending_logins[user_id]['password'] = password
                update.message.reply_text(
                    "🔐 <b>Требуется код Steam Guard</b>\n\n"
                    "Отправьте 5-значный код из приложения Steam:\n"
                    "/cancel - отмена",
                    parse_mode='HTML'
                )
            elif result['success']:
                user_sessions[user_id] = auth_manager
                user_info = auth_manager.get_user_info()
                
                # Сохраняем в базу данных
                db.save_account(
                    user_id, 
                    username, 
                    password,
                    user_info.get('id') if user_info else None,
                    user_info.get('name') if user_info else None
                )
                db.log_login(user_id, username, True)
                
                del pending_logins[user_id]
                update.message.reply_text(
                    f"✅ <b>Вход выполнен и аккаунт сохранен!</b>\n\n"
                    f"👤 {username}\n\n"
                    f"/profile - информация\n"
                    f"/myaccounts - все аккаунты\n"
                    f"/logout - выход",
                    parse_mode='HTML'
                )
            else:
                db.log_login(user_id, username, False, result['message'])
                
                if attempts >= 3:
                    del pending_logins[user_id]
                
                update.message.reply_text(
                    f"❌ <b>Ошибка входа</b>\n\n"
                    f"{result['message']}\n\n"
                    f"Осталось попыток: {3 - attempts}\n\n"
                    f"/login - начать заново\n"
                    f"/cancel - отменить",
                    parse_mode='HTML'
                )
                
        elif step == '2fa':
            if not text.isdigit() or len(text) != 5:
                update.message.reply_text("❌ Код должен быть 5 цифр. Попробуйте снова или /cancel")
                return
            
            auth_manager = pending_logins[user_id]['auth_manager']
            username = pending_logins[user_id]['username']
            password = pending_logins[user_id]['password']
            
            status_msg = update.message.reply_text("⏳ Проверка кода...")
            
            result = auth_manager.login(username, password, text)
            
            try:
                status_msg.delete()
            except:
                pass
            
            if result['success']:
                user_sessions[user_id] = auth_manager
                user_info = auth_manager.get_user_info()
                
                # Сохраняем в базу данных
                db.save_account(
                    user_id, 
                    username, 
                    password,
                    user_info.get('id') if user_info else None,
                    user_info.get('name') if user_info else None
                )
                db.log_login(user_id, username, True)
                
                del pending_logins[user_id]
                update.message.reply_text(
                    f"✅ <b>Вход выполнен и аккаунт сохранен!</b>\n\n"
                    f"👤 {username}\n\n"
                    f"/profile - информация\n"
                    f"/myaccounts - все аккаунты\n"
                    f"/logout - выход",
                    parse_mode='HTML'
                )
            else:
                db.log_login(user_id, username, False, result['message'])
                del pending_logins[user_id]
                update.message.reply_text(
                    f"❌ <b>Ошибка</b>\n\n"
                    f"{result['message']}\n\n"
                    f"/login - новая попытка",
                    parse_mode='HTML'
                )
    else:
        update.message.reply_text(
            "🤖 <b>Команды:</b>\n\n"
            "/login - Войти\n"
            "/profile - Профиль\n"
            "/myaccounts - Мои аккаунты\n"
            "/logout - Выйти\n"
            "/help - Помощь",
            parse_mode='HTML'
        )

def handle_callback(update: Update, context):
    query = update.callback_query
    query.answer()
    
    if query.data == "login_steam":
        login(update, context)

def error_handler(update, context):
    logger.error(f"Error: {context.error}")
    try:
        if update and update.effective_message:
            update.effective_message.reply_text("❌ Ошибка. Попробуйте позже.")
    except:
        pass

def main():
    token = os.getenv('TELEGRAM_BOT_TOKEN')
    
    if not token:
        logger.error("TELEGRAM_BOT_TOKEN не найден")
        print("ОШИБКА: Установите TELEGRAM_BOT_TOKEN")
        return
    
    # Сбрасываем вебхук
    try:
        requests.get(f"https://api.telegram.org/bot{token}/deleteWebhook")
        print("Webhook deleted")
    except:
        pass
    
    # Создаем Updater
    updater = Updater(token, use_context=True)
    dp = updater.dispatcher
    
    # Добавляем обработчики
    dp.add_handler(CommandHandler("start", start))
    dp.add_handler(CommandHandler("help", help_command))
    dp.add_handler(CommandHandler("login", login))
    dp.add_handler(CommandHandler("autologin", autologin))
    dp.add_handler(CommandHandler("logout", logout))
    dp.add_handler(CommandHandler("profile", profile))
    dp.add_handler(CommandHandler("myaccounts", myaccounts))
    dp.add_handler(CommandHandler("switch", switch_account))
    dp.add_handler(CommandHandler("delaccount", delaccount))
    dp.add_handler(CommandHandler("stats", stats))
    dp.add_handler(CommandHandler("cancel", cancel))
    dp.add_handler(CallbackQueryHandler(handle_callback))
    dp.add_handler(MessageHandler(Filters.text & ~Filters.command, handle_message))
    dp.add_error_handler(error_handler)
    
    logger.info("🚀 Бот запущен")
    print("✅ Бот успешно запущен!")
    print("📁 База данных: steam_accounts.db")
    
    updater.start_polling(drop_pending_updates=True)
    updater.idle()

if __name__ == '__main__':
    main()
