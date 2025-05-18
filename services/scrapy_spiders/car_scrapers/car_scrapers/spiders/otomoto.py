import scrapy


class OtomotoSpider(scrapy.Spider):
    name = "otomoto"
    allowed_domains = ["otomoto.pl"]
    start_urls = ["https://otomoto.pl"]

    def parse(self, response):
        pass
