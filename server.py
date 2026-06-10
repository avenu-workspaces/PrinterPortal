from flask import Flask, request, render_template, redirect, url_for
from pyrinter.utils import printer_utils
import random
import json
import threading
import subprocess
from gql import Client, gql
from gql.transport.aiohttp import AIOHTTPTransport
import configparser
from pypdf import PdfReader
import sqlite3
import os
from flask import jsonify
from datetime import datetime, timedelta, date


config = configparser.ConfigParser()
config.read('config.ini')

# List of printers that should be hidden (such as the default windows printer for PDF printing)
blacklisted_printers = [
    p.strip() for p in config['PRINT_CONFIG']['BLACKLISTED_PRINTERS'].split(',')
]

# TODO: add these to config
accepted_extensions = ['pdf', 'png', 'jpg', 'jpeg']

def create_db():
    conn = sqlite3.connect(config['DATABASE']['DB_FILE'])
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS print_jobs (
            job_id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT,
            name TEXT NOT NULL,
            email TEXT NOT NULL,
            file_name TEXT NOT NULL,
            printer TEXT NOT NULL,
            color INTEGER NOT NULL DEFAULT 0,
            pages INTEGER NOT NULL DEFAULT 1,
            copies INTEGER NOT NULL DEFAULT 1,
            duplex INTEGER NOT NULL DEFAULT 0,
            created DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            email TEXT NOT NULL,
            quota INTEGER NOT NULL DEFAULT 100,
            paid_quota INTEGER NOT NULL DEFAULT 0,
            team_id TEXT,
            unlimited BOOLEAN NOT NULL DEFAULT 0,
            last_updated DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """)

    conn.commit()
    conn.close()


app = Flask(__name__)

create_db()

sumatra = config['PATHS']['SUMATRA_PDF_PATH']

def create_user_if_not_exists(user_id, name, email):
    conn = sqlite3.connect(config['DATABASE']['DB_FILE'])
    cursor = conn.cursor()
    cursor.execute("SELECT user_id FROM users WHERE user_id = ?", (user_id,))
    result = cursor.fetchone()
    if not result:
        cursor.execute("INSERT INTO users (user_id, name, email) VALUES (?, ?, ?)", (user_id, name, email))
        conn.commit()
    conn.close()


def get_page_count(path):
    reader = PdfReader(path)
    return len(reader.pages)

def update_quota(user_id, pages_printed):
    conn = sqlite3.connect(config['DATABASE']['DB_FILE'])
    cursor = conn.cursor()
    cursor.execute("SELECT quota, paid_quota FROM users WHERE user_id = ?", (user_id,))
    result = cursor.fetchone()
    if result:
        quota, paid_quota = result
        left = pages_printed
        if quota > 0: # We are allowing to print even if quota is not enough, but we will set it to 0 and not allow further printing until quota is reset
            new_quota = max(quota - pages_printed, 0)
            # left -= quota if pages_printed > quota else 0
            left -= min(quota, pages_printed)
            cursor.execute("UPDATE users SET quota = ?, last_updated = CURRENT_TIMESTAMP WHERE user_id = ?", (new_quota, user_id))
            conn.commit()
        if left <= 0:
            conn.close()
            return "success" if quota > 0 else "quota_exceeded"
        if paid_quota > 0:
            new_paid_quota = max(paid_quota - left, 0)
            cursor.execute("UPDATE users SET paid_quota = ?, last_updated = CURRENT_TIMESTAMP WHERE user_id = ?", (new_paid_quota, user_id))
            conn.commit()
            return "success" if paid_quota > left else "free_quota_exceeded"
        conn.close()
        return "no_quota"
    conn.close()
    return "error"


def add_paid_quota(user_id, amount):
    conn = sqlite3.connect(config['DATABASE']['DB_FILE'])
    cursor = conn.cursor()
    cursor.execute("SELECT paid_quota FROM users WHERE user_id = ?", (user_id,))
    result = cursor.fetchone()
    if result:
        paid_quota = result[0]
        cursor.execute("UPDATE users SET paid_quota = ?, last_updated = CURRENT_TIMESTAMP WHERE user_id = ?", (paid_quota + amount, user_id))
        conn.commit()
        conn.close()
        return True
    conn.close()
    return False


def get_quota(user_id):
    conn = sqlite3.connect(config['DATABASE']['DB_FILE'])
    cursor = conn.cursor()
    cursor.execute("SELECT quota, paid_quota FROM users WHERE user_id = ?", (user_id,))
    result = cursor.fetchone()
    conn.close()
    if result:
        return result[0], result[1]
    else:
        return None, None






def log_thread(email, user_id, name, printer, pages, file_name, color, copies, duplex):
    app.logger.info(f"Logging print job for user_id: {user_id}, email: {email}, printer: {printer}, pages: {pages}, copies: {copies}, duplex: {duplex}")
    conn = sqlite3.connect(config['DATABASE']['DB_FILE'])
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO print_jobs (email, name, user_id, printer, pages, file_name, color, copies, duplex)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (email, name, user_id, printer, pages, file_name, color, copies, duplex))
    conn.commit()
    conn.close()


def log_print(email, user_id, name, printer, pages, file_name, color, copies, duplex):
    threading.Thread(target=log_thread, args=(email, user_id, name, printer, pages, file_name, color, copies, duplex)).start()
    


def print_pdf(file_path, printer_name, copies=1, duplex=True, color=False):
    if copies > 10:
        copies = 10 # Limit the number of copies to prevent abuse
    elif copies < 1:
        copies = 1
    cmd = [
        sumatra,
        "-print-to", printer_name,
        "-silent",
        "-print-settings", f"scale,paper=letter,{copies}x" + (",duplex" if duplex else ",simplex") +  (",monochrome" if not color else ",color"),
        file_path,
    ]
    p = subprocess.run(cmd, capture_output=True, text=True)
    app.logger.info("returncode:", p.returncode)
    app.logger.info("stdout:", p.stdout)
    app.logger.info("stderr:", p.stderr)


def get_printers():
    printers = printer_utils.get_all_printers()
    printers = [p for p in printers if p not in blacklisted_printers]
    return printers

@app.route('/printers', methods=['GET'])
def printers():
    printers = get_printers()
    return json.dumps(printers), 200

def is_admin(token):
    transport = AIOHTTPTransport(url="https://api.optixapp.com/graphql", 
                                headers={"Authorization": "Bearer " + token})
    client = Client(transport=transport)
    query = gql("""
                query {
                    me {
                        user {
                            is_admin
                        }
                    }
                }""")
    result = client.execute(query)
    return result['me']['user']['is_admin']


def get_user_info(token=None, email=None):
    """
    Fetches user information from the Optix API using either an authentication token or an email address.
    """
    if token:
        transport = AIOHTTPTransport(url="https://api.optixapp.com/graphql", 
                                headers={"Authorization": "Bearer " + token})
        client = Client(transport=transport)
        query = gql("""
                    query {
                        me {
                            user {
                                fullname
                                user_id
                                email
                                has_plans
                            }
                        }
                    }""")
        result = client.execute(query)
        user = result['me']['user']
        return {
            "id": user['user_id'],
            "email": user['email'],
            "has_plans": user['has_plans'],
            "name": user['fullname']
        }
    elif email:
        if not '' in email or not '@' in email:
            return None
        transport = AIOHTTPTransport(url="https://api.optixapp.com/graphql",
                                 headers={"Authorization": "Bearer " + config['KEYS']['OPTIX_API']})
        client = Client(transport=transport)
        query = gql("""
                    query {{
                        users(search_by_email: "{email}", limit: 1){{
                            data {{
                                fullname
                                user_id
                                email
                                has_plans
                            }}
                        }}
                    }}""".format(email=email))
        result = client.execute(query)
        user = result['users']['data'][0] if result['users']['data'] else None
        if user:
            return {
                "name": user['fullname'],
                "id": user['user_id'],
                "email": user['email'],
                "has_plans": user['has_plans']
            }
    return None

def is_valid_email(email):

    if not email or not '@' in email:
        return False
    
    transport = AIOHTTPTransport(url="https://api.optixapp.com/graphql",
                                 headers={"Authorization": "Bearer " + config['KEYS']['OPTIX_API']})
    client = Client(transport=transport)
    query = gql("""
                query {{
                    users(search_by_email: "{email}", limit: 1){{
                        data {{
                            has_plans
                        }}
                    }}
                }}""".format(email=email))
    result = client.execute(query)
    has_plans = result['users']['data'][0]['has_plans'] if result['users']['data'] else False
    return has_plans

@app.route('/validate_email', methods=['POST'])
def validate_email():
    data = request.get_json()
    email = data.get('email', '')
    app.logger.info(f"Validating email: {email}")
    has_plans = is_valid_email(email)
    return json.dumps({"valid": has_plans}), 200    

    
@app.route('/update_org_token', methods=['POST'])
def update_org_token():
    data = request.form
    if data['event'] == 'organization_token_updated':
        new_token = data['organization_token']
        config['KEYS']['OPTIX_API'] = new_token
        config.write(open('config.ini', 'w'))
        return jsonify({'message': 'Organization token updated'}), 200


@app.route('/', methods=['GET', 'POST'])
def index():
    if request.method == 'POST':
        file = request.files['file']
        file_path = f"C:\\Users\\avenu\\Desktop\\printer\\temp\\{random.randint(1000,9999)}_{file.filename}"
        copies = int(request.form.get('copies', 1))
        token = request.form.get('token', '')

        if not file.filename.split('.')[-1] in accepted_extensions:
            return render_template('index.html', error_message="File type must be one of the following " + ', '.join(accepted_extensions))

        duplex = request.form.get('duplex', 'off') == 'true'
        app.logger.info(f"Received print job: {file.filename}, copies: {copies}, duplex: {duplex}")

        
        user_info = get_user_info(token=token, email=request.form.get('email', None))
        has_plans = user_info and user_info['has_plans']


        if config['SERVER']['ENABLE_EMAIL_AUTH'] == 'True':
            app.logger.info(f"User has plans: {has_plans}, email auth enabled: {config['SERVER']['ENABLE_EMAIL_AUTH']}, email provided: {'email' in request.form}, valid email: {is_valid_email(request.form['email']) if 'email' in request.form else 'N/A'}")
        if not has_plans:
            app.logger.info("User does not have a plan, rejecting print job")
            return render_template('index.html', error_message="You do not have an active plan to print with. Please contact help@avenuworkspaces.com to purchase a plan.")
        
        create_user_if_not_exists(user_info['id'], user_info['name'], user_info['email'])

        
        app.logger.info(f"Saving file to: {file_path}")
        file.save(file_path)
        printer_name = request.form['printer']
        color = request.form.get('color', 'off') == 'true'
        app.logger.info(f"Printing file: {file_path} to printer: {printer_name}")
        pages = get_page_count(file_path) if file_path.lower().endswith('.pdf') else 1

        quota_status = update_quota(user_info['id'], pages * copies)

        warning_message = None
        
        if quota_status in ['error', 'no_quota']:
            app.logger.info("User quota exceeded, rejecting print job")
            return render_template('index.html', error_message="Your printing quota has been exceeded. Please contact help@avenuworkspaces.com to request additional quota.")
        
        if quota_status == 'free_quota_exceeded':
            warning_message="You have exceeded your free printing quota. Please contact help@avenuworkspaces.com to request additional quota."
        if quota_status == 'paid_quota_exceeded':
            warning_message="You have ran out of your paid printing quota. Please contact help@avenuworkspaces.com to request additional quota."

        if pages == 1:
            duplex = False

        print_pdf(file_path, printer_name, copies=copies, duplex=duplex, color=color)


        log_print(
            name=user_info['name'],
            email=user_info['email'],
            user_id=user_info['id'],
            printer=printer_name,
            pages=pages,
            file_name=file.filename,
            color=color,
            copies=copies,
            duplex=duplex
        )


        return render_template('index.html', success_message="Your file has successfully been sent to the printer. Please allow time for it to print.", warning_message=warning_message)
    return render_template('index.html')
                           


# |------------------------------- STATS -----------------------------| #
# @app.route('/pages_printed', methods=['GET'])
def stats(start_date, end_date):
    # start_date = request.args.get('start_date')
    # end_date = request.args.get('end_date')

    if not start_date or not end_date:
        app.logger.error("Missing start_date or end_date parameters")
        # return jsonify({"error": "start_date and end_date are required"}), 400
        return None

    try:
        start_str = datetime.strptime(start_date, "%Y-%m-%d").strftime("%Y-%m-%d 00:00:00")
        end_str = datetime.strptime(end_date, "%Y-%m-%d").strftime("%Y-%m-%d 23:59:59")
    except ValueError:
        app.logger.error(f"Invalid date format: start_date={start_date}, end_date={end_date}")
        # return jsonify({"error": "Invalid date format. Use YYYY-MM-DD"}), 400
        return None


    conn = sqlite3.connect(config['DATABASE']['DB_FILE'])
    cursor = conn.cursor()

    cursor.execute("""
        SELECT 
            J.user_id,
            U.name,
            SUM(
                CASE 
                    WHEN J.duplex = 1 THEN ((J.pages * J.copies) / 2.0)
                    ELSE J.pages * J.copies
                END
            ) AS total_pages,
            U.quota,
            U.paid_quota
        FROM print_jobs J
        JOIN users U ON J.user_id = U.user_id
        WHERE datetime(J.created) >= datetime(?)
        AND datetime(J.created) < datetime(?)
        GROUP BY J.user_id, U.name, U.quota, U.paid_quota
        ORDER BY total_pages DESC;
    """, (start_str, end_str))

    results = cursor.fetchall()
    conn.close()


    stats = [{"user_id": row[0], "name": row[1], "pages_printed": row[2], "quota": row[3], "paid_quota": row[4]} for row in results]

    
    return stats

@app.route('/adjust_quota', methods=['POST'])
def adjust_quota():
    token = request.form.get('token', '')
    if not token or not is_admin(token):
        return redirect(url_for('index'))
    
    user_id = request.form.get('user_id')
    new_quota = int(request.form.get('quantity', 0))

    if add_paid_quota(user_id, new_quota):
        return redirect(url_for('stats_page', token=token, message="Quota updated successfully"))
    else:
        return redirect(url_for('stats_page', token=token, message="Failed to update quota"))


@app.route('/stats', methods=['GET', 'POST'])
def stats_page():
    message = request.args.get('message', None)
    token = request.form.get('token', '') if request.method == 'POST' else request.args.get('token', '')
    if not token or not is_admin(token):
        return redirect(url_for('index'))
    if request.method == 'POST':
        start_date = request.form.get('startDate')
        end_date = request.form.get('endDate')
        stats_data = stats(start_date, end_date)
        return render_template('stats.html', stats=stats_data, start_date=start_date, end_date=end_date, token=token, message=message)
    end_date = date.today()
    start_date = date(end_date.year, end_date.month, 1)
    stats_data = stats(start_date.strftime("%Y-%m-%d"), end_date.strftime("%Y-%m-%d"))
    return render_template('stats.html', stats=stats_data, start_date=start_date.strftime("%Y-%m-%d"), end_date=end_date.strftime("%Y-%m-%d"), token=token, message=message)




if not os.path.exists(config['PATHS']['TEMP_DIR']):
    os.makedirs(config['PATHS']['TEMP_DIR'])

if __name__ == '__main__':
    app.run(host=config['SERVER']['HOST'], port=int(config['SERVER']['PORT']))
    
