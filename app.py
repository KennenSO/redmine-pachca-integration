# -*- coding: utf-8 -*-
import json
import traceback
import sys
import os
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse
import mysql.connector
import requests
from dotenv import load_dotenv 

# === Загрузка переменных из .env ===
load_dotenv()  # Загружает .env файл в os.environ

# === Настройки ===
PORT = int(os.getenv("PORT", 8080))
REDMINE_WEBHOOK_PATH = '/webhook'

# MySQL настройки (теперь из .env)
MYSQL_CONFIG = {
    'host': os.getenv('MYSQL_HOST', '127.0.0.1'),
    'database': os.getenv('MYSQL_DATABASE', 'redmine'),
    'user': os.getenv('MYSQL_USER', 'root'),
    'password': os.getenv('MYSQL_PASSWORD', ''),
    'port': int(os.getenv('MYSQL_PORT', 3306)),
    'charset': 'utf8mb4',
    'collation': 'utf8mb4_unicode_ci',
    'use_pure': True,
    'auth_plugin': 'mysql_native_password'
}

# Pachca Incoming Webhook URL
PACHCA_WEBHOOK_URL = os.getenv('PACHCA_WEBHOOK_URL', '')

# === Проверка обязательных переменных ===
if not PACHCA_WEBHOOK_URL:
    print("🛑 ОШИБКА: PACHCA_WEBHOOK_URL не задан в .env!")
    sys.exit(1)

if not MYSQL_CONFIG['password']:
    print("🛑 ОШИБКА: MYSQL_PASSWORD не задан в .env!")
    sys.exit(1)

print("🔍 Проверка подключения к MySQL...")
try:
    conn = mysql.connector.connect(**MYSQL_CONFIG)
    conn.close()
    print("✅ Подключение к MySQL успешно!")
except mysql.connector.Error as e:
    print(f"❌❌ КРИТИЧЕСКАЯ ОШИБКА MySQL: {e}")
    sys.exit(1)
except Exception as e:
    print(f"❌❌ Неизвестная ошибка: {e}")
    sys.exit(1)

# === Логика ===
def get_latest_issue():
    """Запрос к MySQL: последняя заявка за день с tracker_id = 10 и status_id = 1"""
    conn = None
    try:
        conn = mysql.connector.connect(**MYSQL_CONFIG)
    except mysql.connector.Error as e:
        print(f"❌❌ КРИТИЧЕСКАЯ ОШИБКА MySQL: Невозможно подключиться к базе: {e}")
        return None
    except Exception as e:
        print(f"❌❌ Неизвестная ошибка при подключении к MySQL: {e}")
        return None

    try:
        cursor = conn.cursor(dictionary=True)
        query = """
            SELECT i.id, i.subject, p.name AS company_name
            FROM issues i
            JOIN projects p ON i.project_id = p.id
            WHERE i.status_id = 1
              AND i.created_on >= CURDATE() - INTERVAL 1 DAY
              AND i.tracker_id = 10
            ORDER BY i.created_on DESC
            LIMIT 1
        """
        cursor.execute(query)
        result = cursor.fetchone()
        cursor.close()
        return result
    except Exception as e:
        print(f"❌ Ошибка запроса к MySQL: {e}")
        return None
    finally:
        if conn and conn.is_connected():
            conn.close()

def send_to_pachca(issue_id, company_name, subject):
    """Отправка сообщения в Pachca через Incoming Webhook — с кликабельной ссылкой"""
    redmine_base_url = "http://hd.integrosoft.ru/issues/"
    issue_link = f"{redmine_base_url}{issue_id}"

    # Формируем 4 строки: заголовок, компания, тема, ссылка — каждая на новой строке
    lines = [
        f"📌 **Новая заявка в Redmine**",
        f"🔗 [#{issue_id}]({issue_link})",
        f"🏢 {company_name}",
        f"📋 {subject}",
    ]

    # Убираем пустые строки (на всякий случай)
    message = "\n".join(line for line in lines if line.strip())

    payload = {
        "message": message
    }

    print(f"📤 Отправляю в Pachca:\n{message}\n{'─' * 50}")
    try:
        response = requests.post(PACHCA_WEBHOOK_URL, json=payload, timeout=10)
        print(f"📡 Ответ Pachca: {response.status_code} | {response.text}")
        if response.status_code == 200 and response.text.strip() == "OK":
            print(f"✅ Успешно отправлено в Pachca!")
            return True
        else:
            print(f"❌ Ошибка Pachca: {response.status_code} - {response.text}")
            return False
    except Exception as e:
        print(f"❌ Ошибка сети при отправке в Pachca: {e}")
        return False

class WebhookHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        parsed_path = urlparse(self.path)
        if parsed_path.path != REDMINE_WEBHOOK_PATH:
            self.send_response(404)
            self.send_header('Content-Type', 'text/plain')
            self.end_headers()
            self.wfile.write(b'Not found (POST)')
            return

        try:
            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length).decode('utf-8')
            data = json.loads(body)
            payload = data.get('payload', {})
            action = payload.get('action')
            issue = payload.get('issue', {})
            tracker_id = issue.get('tracker', {}).get('id')

            print(f"📩 Получен вебхук: action={action}, tracker_id={tracker_id}")

            # Обрабатываем только новые заявки с tracker_id = 10
            if action == 'opened' and tracker_id == 10:
                print("🔄 Запуск поиска последней заявки в MySQL...")
                issue_data = get_latest_issue()

                if issue_data:
                    issue_id = issue_data['id']
                    subject = issue_data['subject']
                    company_name = issue_data['company_name']

                    # Отправляем данные в Pachca — с кликабельной ссылкой!
                    if send_to_pachca(issue_id, company_name, subject):
                        self.send_response(200)
                        self.send_header('Content-Type', 'text/plain')
                        self.end_headers()
                        self.wfile.write(b'Success')
                        return
                    else:
                        self.send_response(500)
                        self.send_header('Content-Type', 'text/plain')
                        self.end_headers()
                        self.wfile.write(b'Failed to send to Pachca')
                        return
                else:
                    print("⚠️ Не найдена заявка в MySQL (возможно, нет новых за день)")
                    self.send_response(200)
                    self.send_header('Content-Type', 'text/plain')
                    self.end_headers()
                    self.wfile.write(b'No matching issue found')
                    return
            else:
                print(f"⏭️ Пропущено: action={action}, tracker_id={tracker_id}")
                self.send_response(200)
                self.send_header('Content-Type', 'text/plain')
                self.end_headers()
                self.wfile.write(b'No action')
                return

        except json.JSONDecodeError as e:
            print(f"❌ JSON ошибка: {e}")
            self.send_response(400)
            self.send_header('Content-Type', 'text/plain')
            self.end_headers()
            self.wfile.write(b'Invalid JSON')
        except KeyError as e:
            print(f"❌ Ключ не найден: {e}")
            self.send_response(400)
            self.send_header('Content-Type', 'text/plain')
            self.end_headers()
            self.wfile.write(b'Missing key in JSON')
        except Exception as e:
            print(f"❌ Неожиданная ошибка: {traceback.format_exc()}")
            self.send_response(500)
            self.send_header('Content-Type', 'text/plain')
            self.end_headers()
            self.wfile.write(b'Internal server error')

    def log_message(self, format, *args):
        # Отключаем стандартные логи сервера
        return

if __name__ == "__main__":
    print("🚀 Запуск сервера Redmine → Pachca интеграции...")
    print(f" Слушаем на http://localhost:{PORT}{REDMINE_WEBHOOK_PATH}")
    print(f" MySQL: {MYSQL_CONFIG['database']} @ {MYSQL_CONFIG['host']}")
    print(f" PACHCA_WEBHOOK_URL: ✅ Установлен (длина: {len(PACHCA_WEBHOOK_URL)} символов)")

    # Проверка: если URL не изменился — предупреждение
    if "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx" in PACHCA_WEBHOOK_URL:
        print("\n🛑 ОШИБКА: Замените PACHCA_WEBHOOK_URL на настоящий URL из Pachca!")
        print(" → Откройте чат в Pachca → Настройки → Вебхуки → Скопируйте URL вебхука")
        sys.exit(1)

    server = HTTPServer(("", PORT), WebhookHandler)
    try:
        print("✅ Сервер запущен. Ожидаем вебхуки...")
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n🛑 Сервер остановлен.")
        server.shutdown()
