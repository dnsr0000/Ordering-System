import pytest

from app.core.errors import ValidationError
from app.domain.menu.service import MenuDomainService
from app.services.order_service import (
    create_order,
    transition_order_status,
    validate_cancel_allowed,
)


def test_order_domain_smoke():
    order = create_order({"items": [{"price": 100, "quantity": 2}]}, 7)
    assert order["status"] == "pending"
    assert order["total"] == 200.0

    updated = transition_order_status({"status": "pending"}, "paid")
    assert updated["status"] == "paid"

    try:
        validate_cancel_allowed({"status": "completed"})
    except ValidationError as exc:
        assert "不可取消" in str(exc)
    else:
        raise AssertionError("Completed order should not be cancellable")


def test_menu_domain_reward_validation():
    normalized = MenuDomainService.validate_reward_status(100, True, 80, 20)
    assert normalized == {"is_reward": True, "reward_points": 80, "reward_discount_points": 20}

    with pytest.raises(ValidationError):
        MenuDomainService.validate_reward_status(100, True, 200, 0)


def test_menu_domain_combo_validation():
    validated = MenuDomainService.validate_combo_submission("超值套餐", main_id=7, additional_price=50, description="描述")
    assert validated["name"] == "超值套餐"
    assert validated["additional_price"] == 50
    with pytest.raises(ValidationError):
        MenuDomainService.validate_combo_submission("", main_id=7)


def test_menu_domain_import_number_validation():
    assert MenuDomainService.validate_import_number("NT$120", "單價", "牛肉飯", 1000, to_int=int) == 120
    with pytest.raises(ValidationError):
        MenuDomainService.validate_import_number("-5", "單價", "牛肉飯", 1000, to_int=int)


def test_order_domain_transition_validation():
    OrderDomainService = __import__('app.domain.order.service', fromlist=['OrderDomainService']).OrderDomainService
    with pytest.raises(ValidationError):
        OrderDomainService.validate_transition("completed", "cancelled")
    assert OrderDomainService.validate_transition("pending", "paid") == "paid"


def test_order_domain_payload_validation():
    payload = {"items": [{"price": 100, "quantity": 2}, {"price": 50, "quantity": 1}]}
    validated = __import__('app.domain.order.service', fromlist=['OrderDomainService']).OrderDomainService.validate_payload(payload, 7)
    assert validated["total"] == 250.0
    with pytest.raises(ValidationError):
        __import__('app.domain.order.service', fromlist=['OrderDomainService']).OrderDomainService.validate_payload({"items": [{"price": 100, "quantity": 0}]}, 7)


def test_order_domain_cancel_policy_validation():
    OrderDomainService = __import__('app.domain.order.service', fromlist=['OrderDomainService']).OrderDomainService
    OrderDomainService.validate_cancel_policy({"status": "paid", "is_paid": True, "total_price": 150})
    with pytest.raises(ValidationError):
        OrderDomainService.validate_cancel_policy({"status": "completed", "is_paid": True, "total_price": 150})


def test_order_domain_refund_policy_validation():
    OrderDomainService = __import__('app.domain.order.service', fromlist=['OrderDomainService']).OrderDomainService
    assert OrderDomainService.validate_refund_policy({"payment_status": "paid", "refundable_amount": 120}) == 120
    with pytest.raises(ValidationError):
        OrderDomainService.validate_refund_policy({"payment_status": "pending", "refundable_amount": 120})


def test_order_domain_payment_method_validation():
    OrderDomainService = __import__('app.domain.order.service', fromlist=['OrderDomainService']).OrderDomainService
    assert OrderDomainService.validate_payment_method("credit_card") == "credit_card"
    with pytest.raises(ValidationError):
        OrderDomainService.validate_payment_method("bitcoin")


def test_order_domain_payment_status_validation():
    OrderDomainService = __import__('app.domain.order.service', fromlist=['OrderDomainService']).OrderDomainService
    assert OrderDomainService.validate_payment_status("PAID") == "paid"
    assert OrderDomainService.validate_payment_status("") == ""
    with pytest.raises(ValidationError):
        OrderDomainService.validate_payment_status("invalid_status")


def test_order_domain_checkout_total_validation():
    OrderDomainService = __import__('app.domain.order.service', fromlist=['OrderDomainService']).OrderDomainService
    assert OrderDomainService.validate_checkout_total(200, 50, service_charge=20, delivery_fee=10) == 180.0
    with pytest.raises(ValidationError):
        OrderDomainService.validate_checkout_total(100, 200)


def test_order_domain_expected_total_validation():
    OrderDomainService = __import__('app.domain.order.service', fromlist=['OrderDomainService']).OrderDomainService
    assert OrderDomainService.validate_expected_total(200, 50, service_charge=20, delivery_fee=10, expected_total=180) == 180.0
    with pytest.raises(ValidationError):
        OrderDomainService.validate_expected_total(200, 50, service_charge=20, delivery_fee=10, expected_total=170)


def test_member_domain_validation_rules():
    MemberDomainService = __import__('app.domain.member.service', fromlist=['MemberDomainService']).MemberDomainService
    assert MemberDomainService.calculate_points_earned(125, 10, 1.0) == 12
    assert MemberDomainService.validate_coupon_code("  save-10  ") == "SAVE-10"
    assert MemberDomainService.calculate_discount(200, 0.15) == 30.0

    MemberDomainService.validate_points_deduction(50, 30)
    with pytest.raises(ValidationError):
        MemberDomainService.validate_points_deduction(20, 30)
    with pytest.raises(ValidationError):
        MemberDomainService.validate_coupon_code("  ")


def test_member_service_wrapper_contract():
    from app.services.member_service import (calculate_points_earned, validate_points_deduction,
                                            validate_coupon_code, calculate_discount)
    assert calculate_points_earned(125, 10, 1.0) == 12
    assert validate_coupon_code(" save-10 ") == "SAVE-10"
    assert calculate_discount(200, 0.15) == 30.0
    validate_points_deduction(50, 30)
    with pytest.raises(ValidationError):
        validate_points_deduction(20, 30)


def test_pricing_domain_validation_rules():
    PricingDomainService = __import__('app.domain.pricing.service', fromlist=['PricingDomainService']).PricingDomainService
    assert PricingDomainService.calculate_service_charge(1000, 10) == 100
    assert PricingDomainService.calculate_discount(200, 0.1) == 20.0
    assert PricingDomainService.validate_checkout_total(200, 30, service_charge=15, delivery_fee=10) == 195.0
    with pytest.raises(ValidationError):
        PricingDomainService.calculate_service_charge(100, -1)
    with pytest.raises(ValidationError):
        PricingDomainService.validate_checkout_total(100, 200)


def test_lifecycle_domain_validation_rules():
    LifecycleDomainService = __import__('app.domain.lifecycle.service', fromlist=['LifecycleDomainService']).LifecycleDomainService
    assert LifecycleDomainService.validate_transition('Pending', 'Completed') == 'completed'
    assert LifecycleDomainService.validate_refund_policy({'payment_status': 'paid', 'refundable_amount': 120}) == 120.0
    with pytest.raises(ValidationError):
        LifecycleDomainService.validate_transition('Completed', 'Pending')


if __name__ == "__main__":
    test_order_domain_smoke()
    test_menu_domain_reward_validation()
    test_menu_domain_combo_validation()
    test_menu_domain_import_number_validation()
    print("domain smoke tests passed")
