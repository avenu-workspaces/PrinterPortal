import sqlite3
import configparser

config = configparser.ConfigParser()
config.read('config.ini')


def reset_quotas():
    conn = sqlite3.connect('printer.sqlite')
    cursor = conn.cursor()
    cursor.execute(f'UPDATE users SET quota = {config["PRINT_CONFIG"]["MONTHLY_QUOTA"]}')
    conn.commit()
    conn.close()

if __name__ == '__main__':
    reset_quotas()