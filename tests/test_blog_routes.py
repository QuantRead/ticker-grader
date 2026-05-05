from fastapi.testclient import TestClient

from server import app


client = TestClient(app)


def test_blog_index_serves_article_link():
    response = client.get("/blog")

    assert response.status_code == 200
    assert "QuantRead Blog" in response.text
    assert "/blog/how-do-i-know-if-a-stock-is-actually-a-good-day-trade-setup" in response.text


def test_first_blog_article_serves_canonical_page():
    response = client.get("/blog/how-do-i-know-if-a-stock-is-actually-a-good-day-trade-setup")

    assert response.status_code == 200
    assert "How Do I Know If a Stock Is Actually a Good Day Trade Setup?" in response.text
    assert "https://quantread.app/blog/how-do-i-know-if-a-stock-is-actually-a-good-day-trade-setup" in response.text


def test_robots_points_to_sitemap():
    response = client.get("/robots.txt")

    assert response.status_code == 200
    assert "Sitemap: https://quantread.app/sitemap.xml" in response.text


def test_sitemap_includes_blog_urls():
    response = client.get("/sitemap.xml")

    assert response.status_code == 200
    assert "https://quantread.app/blog" in response.text
    assert "https://quantread.app/blog/how-do-i-know-if-a-stock-is-actually-a-good-day-trade-setup" in response.text
