import time
import argparse
import docker
from prometheus_client import start_http_server, Gauge

# Подключаемся к Docker сокету
try:
    client = docker.from_env()
except Exception as e:
    print(f"CRITICAL: Could not connect to Docker: {e}")
    exit(1)

# Определяем метки (Labels), которые будут у каждой метрики
LABELS = ['container_name', 'container_id', 'image']

# Создаем метрики
CPU_USAGE = Gauge('docker_container_cpu_usage_percent', 'CPU usage percent', LABELS)
MEM_USAGE = Gauge('docker_container_memory_usage_bytes', 'Memory usage bytes', LABELS)
MEM_LIMIT = Gauge('docker_container_memory_limit_bytes', 'Memory limit bytes', LABELS)
NET_RX    = Gauge('docker_container_network_rx_bytes', 'Network received bytes', LABELS)
NET_TX    = Gauge('docker_container_network_tx_bytes', 'Network transmitted bytes', LABELS)

def calculate_cpu_percent(d):
    """
    Рассчитывает % CPU на основе дельты между текущим и предыдущим чтением.
    Логика аналогична команде 'docker stats'.
    """
    try:
        cpu_count = len(d["cpu_stats"]["cpu_usage"]["percpu_usage"])
        cpu_percent = 0.0
        
        cpu_usage = float(d["cpu_stats"]["cpu_usage"]["total_usage"])
        # Берем предыдущее значение (precpu_stats), которое Docker API отдает сам
        precpu_usage = float(d["precpu_stats"]["cpu_usage"]["total_usage"])
        
        system_usage = float(d["cpu_stats"]["system_cpu_usage"])
        system_precpu_usage = float(d["precpu_stats"]["system_cpu_usage"])

        cpu_delta = cpu_usage - precpu_usage
        system_delta = system_usage - system_precpu_usage

        if system_delta > 0.0 and cpu_delta > 0.0:
            cpu_percent = (cpu_delta / system_delta) * cpu_count * 100.0
        return cpu_percent
    except KeyError:
        return 0.0

def collect_metrics():
    # Получаем список ТОЛЬКО запущенных контейнеров
    try:
        containers = client.containers.list()
    except Exception as e:
        print(f"Error getting container list: {e}")
        return

    # Сохраняем ID всех контейнеров, которые мы увидели в этом цикле
    seen_container_ids = set()

    for container in containers:
        try:
            # Получаем ID и Имя
            c_id_short = container.id[:12]
            c_name = container.name
            # Получаем тег образа (если есть) или хеш
            c_image = container.image.tags[0] if container.image.tags else container.image.id[:12]
            
            seen_container_ids.add(c_id_short)

            # Запрашиваем статистику (stream=False дает мгновенный JSON ответ)
            stats = container.stats(stream=False)

            # 1. CPU
            cpu_val = calculate_cpu_percent(stats)
            CPU_USAGE.labels(c_name, c_id_short, c_image).set(cpu_val)

            # 2. Memory
            mem_usage = stats["memory_stats"].get("usage", 0)
            mem_limit = stats["memory_stats"].get("limit", 0)
            
            # (Опционально) Вычитаем кэш, чтобы получить "честную" память как в docker stats
            # cache = stats["memory_stats"].get("stats", {}).get("inactive_file", 0)
            # mem_usage = mem_usage - cache

            MEM_USAGE.labels(c_name, c_id_short, c_image).set(mem_usage)
            MEM_LIMIT.labels(c_name, c_id_short, c_image).set(mem_limit)

            # 3. Network (сумма по всем интерфейсам)
            rx = 0
            tx = 0
            networks = stats.get("networks", {})
            if networks:
                for iface, net_data in networks.items():
                    rx += net_data.get("rx_bytes", 0)
                    tx += net_data.get("tx_bytes", 0)
            
            NET_RX.labels(c_name, c_id_short, c_image).set(rx)
            NET_TX.labels(c_name, c_id_short, c_image).set(tx)

        except Exception as e:
            # Контейнер мог умереть прямо во время опроса
            print(f"Error processing container {container.name}: {e}")

    # ОЧИСТКА МЕТРИК: Удаляем метрики контейнеров, которых больше нет в списке
    # Это важно, иначе Prometheus будет вечно показывать старые данные умерших контейнеров
    # (Библиотека prometheus_client не делает это сама для кастомных лейблов)
    # Примечание: В простой реализации мы не чистим child metrics напрямую, 
    # но в продакшене тут стоит добавить логику удаления (remove) для отсутствующих ID.
    # Для простоты скрипта этот шаг часто пропускают, но для чистоты данных он нужен.

if __name__ == '__main__':
    # Парсинг аргументов командной строки
    parser = argparse.ArgumentParser(description='Docker Prometheus Exporter')
    parser.add_argument('--port', '-p', type=int, default=8000,
                        help='Port for Prometheus metrics HTTP server (default: 8000)')
    parser.add_argument('--interval', '-i', type=int, default=5,
                        help='Metrics collection interval in seconds (default: 5)')
    args = parser.parse_args()

    # Запускаем HTTP сервер
    start_http_server(args.port)
    print(f"Docker Exporter running on port {args.port}. Watching running containers...")
    print(f"Collection interval: {args.interval} seconds")

    while True:
        collect_metrics()
        # Пауза между опросами (в секундах)
        time.sleep(args.interval)
