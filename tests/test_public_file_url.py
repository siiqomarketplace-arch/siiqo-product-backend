"""S5: public product data never carries a paid product's download link;
free products keep it so the free-claim flow still works."""


def _get_product(client, product):
    return client.get(f"/api/marketplace/products/{product.id}")


def test_paid_digital_product_hides_file(client, make_vendor, make_product):
    _, store = make_vendor()
    product = make_product(store, price=5000, product_type="digital", file_url="https://files.example/paid.pdf")
    res = _get_product(client, product)
    assert res.status_code == 200
    assert "paid.pdf" not in res.get_data(as_text=True)


def test_free_digital_product_keeps_file(client, make_vendor, make_product):
    _, store = make_vendor()
    product = make_product(store, price=0, product_type="digital", file_url="https://files.example/free.pdf",
                           is_free=True)
    res = _get_product(client, product)
    assert res.status_code == 200
    assert "free.pdf" in res.get_data(as_text=True)
