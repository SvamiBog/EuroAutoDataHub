# services/scrapy_spiders/car_scrapers/car_scrapers/pipelines.py
import json
import logging
from itemadapter import ItemAdapter
from kafka import KafkaProducer
from kafka.errors import KafkaError
from .items import ParsedAdItem, ActiveIdsItem


class KafkaPipeline:
    def __init__(self, kafka_bootstrap_servers, kafka_topic_ads, kafka_topic_active_ids, kafka_producer_config=None):
        self.kafka_bootstrap_servers = kafka_bootstrap_servers
        self.kafka_topic_ads = kafka_topic_ads
        self.kafka_topic_active_ids = kafka_topic_active_ids
        self.producer = None
        self.kafka_producer_config = kafka_producer_config if kafka_producer_config else {}
        self.logger = logging.getLogger(self.__class__.__name__)

    @classmethod
    def from_crawler(cls, crawler):
        # Получаем настройки из settings.py Scrapy
        kafka_bootstrap_servers = crawler.settings.get('KAFKA_BOOTSTRAP_SERVERS')
        kafka_topic_ads = crawler.settings.get('KAFKA_TOPIC_ADS')
        kafka_topic_active_ids = crawler.settings.get('KAFKA_TOPIC_ACTIVE_IDS')

        kafka_producer_config = crawler.settings.get('KAFKA_PRODUCER_CONFIG', {})

        if not kafka_bootstrap_servers or not kafka_topic_ads or not kafka_topic_active_ids:
            raise ValueError(
                "Один из обязательных параметров Kafka (BOOTSTRAP_SERVERS, TOPIC_ADS, TOPIC_ACTIVE_IDS) не настроен в settings.py")

        return cls(
            kafka_bootstrap_servers=kafka_bootstrap_servers,
            kafka_topic_ads=kafka_topic_ads,
            kafka_topic_active_ids=kafka_topic_active_ids,
            kafka_producer_config=kafka_producer_config
        )

    def open_spider(self, spider):
        try:
            self.producer = KafkaProducer(
                bootstrap_servers=self.kafka_bootstrap_servers,
                value_serializer=lambda v: json.dumps(v).encode('utf-8'),
                **self.kafka_producer_config
            )
            self.logger.info(f"KafkaProducer подключен к {self.kafka_bootstrap_servers}")
        except KafkaError as e:
            self.logger.error(f"Не удалось подключиться к Kafka: {e}")


    def close_spider(self, spider):
        if hasattr(spider, 'scraped_ids') and hasattr(spider, 'name'):
            if spider.scraped_ids: # Проверяем, что есть ID для отправки
                self.logger.info(f"KafkaPipeline: Паук {spider.name} завершает работу. Собрано {len(spider.scraped_ids)} уникальных ID для отправки из close_spider.")
                try:
                    # Создаем экземпляр ActiveIdsItem
                    active_ids_item = ActiveIdsItem()
                    active_ids_item['source_name'] = 'otomoto.pl'
                    active_ids_item['ad_ids'] = list(spider.scraped_ids)

                    # Вызываем self.process_item для отправки этого item через существующую логику
                    # Убеждаемся, что продюсер еще активен
                    if self.producer:
                        self.logger.info(f"KafkaPipeline: Передача ActiveIdsItem в self.process_item из close_spider для {spider.name}.")
                        self.process_item(active_ids_item, spider) # process_item сам залогирует результат отправки
                    else:
                        self.logger.warning(f"KafkaPipeline: KafkaProducer не был доступен в close_spider для отправки ActiveIdsItem через process_item для {spider.name}.")

                except Exception as e:
                    self.logger.error(f"KafkaPipeline: Ошибка при подготовке/обработке ActiveIdsItem из close_spider для {spider.name}: {e}", exc_info=True)
            else:
                self.logger.info(f"KafkaPipeline: У паука {spider.name} нет scraped_ids для отправки из close_spider.")
        else:
            self.logger.warning(f"KafkaPipeline: Паук {spider.name} не имеет атрибутов 'scraped_ids' или 'name'. Невозможно отправить ActiveIdsItem из close_spider.")

        if self.producer:
            try:
                self.logger.info(f"KafkaPipeline: Flushing and closing KafkaProducer для паука {spider.name}.")
                self.producer.flush()
                self.producer.close()
                self.logger.info(f"KafkaPipeline: KafkaProducer для паука {spider.name} успешно закрыт.")
            except KafkaError as e:
                self.logger.error(f"KafkaPipeline: Ошибка Kafka при закрытии KafkaProducer для паука {spider.name}: {e}")
            except Exception as e:
                self.logger.error(f"KafkaPipeline: Неожиданная ошибка при закрытии KafkaProducer для паука {spider.name}: {e}", exc_info=True)



    def process_item(self, item, spider):
        if not self.producer:
            self.logger.warning(f"KafkaProducer не инициализирован. Элемент не будет отправлен.")
            return item

        item_dict = ItemAdapter(item).asdict()

        try:
            if isinstance(item, ParsedAdItem):
                topic = self.kafka_topic_ads
                message_key = item_dict.get('source_ad_id', item_dict.get('url'))
                if message_key:
                    message_key = message_key.encode('utf-8')

                future = self.producer.send(topic, key=message_key, value=item_dict)

            elif isinstance(item, ActiveIdsItem):
                topic = self.kafka_topic_active_ids
                # В качестве ключа можно использовать имя паука/источника
                message_key = item_dict.get('source_name').encode('utf-8')

                future = self.producer.send(topic, key=message_key, value=item_dict)
                self.logger.info(f"Список из {len(item_dict.get('ad_ids', []))} активных ID отправлен в топик {topic}")

            else:
                # Если появится какой-то другой тип item, просто его пропустим
                self.logger.warning(f"Неизвестный тип item: {type(item)}. Элемент не будет отправлен в Kafka.")
                return item

        except KafkaError as e:
            self.logger.error(f"Ошибка при отправке элемента в Kafka: {e}")
        except Exception as e:
            self.logger.error(f"Неожиданная ошибка в KafkaPipeline: {e}")

        return item
