from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from uuid import UUID, uuid5
from zoneinfo import ZoneInfo

from sqlalchemy import create_engine, delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.core.config import settings
from app.core.doctor_names import normalize_doctor_display_name, normalize_doctor_name_parts
from app.models.appointment import APPOINTMENT_SOURCES, APPOINTMENT_STATUSES, Appointment
from app.models.booking_settings import BookingSettings
from app.models.branch import Branch
from app.models.doctor import Doctor
from app.models.doctor_branch import DoctorBranch
from app.models.doctor_service import DoctorService
from app.models.patient import Patient
from app.models.service import Service
from app.models.staff import Staff
from app.models.working_hours import BranchWorkingHour, DoctorWorkingHour
from app.models.workspace import Workspace

FIXTURE_VERSION = "realistic-aesthetic-clinic-v1"
FIXTURE_NAMESPACE = "tia-ai:realistic-aesthetic-clinic:v1"
CAIRO_TZ = ZoneInfo("Africa/Cairo")

# Weekday convention follows Python datetime.weekday(): Monday=0 ... Sunday=6.
BRANCHES = [
    {
        "key": "nasr-city",
        "name": "فرع مدينة نصر",
        "code": "nasr-city",
        "phone": "+200000210001",
        "email": "nasr-city@tiaai.online",
        "address_line1": "عباس العقاد - مدينة نصر",
        "city": "Cairo",
        "hours": {
            0: [("10:00", "22:00")],
            1: [("10:00", "22:00")],
            2: [("10:00", "22:00")],
            3: [("10:00", "22:00")],
            4: [("14:00", "22:00")],
            5: [("10:00", "22:00")],
            6: [("10:00", "22:00")],
        },
    },
    {
        "key": "new-cairo",
        "name": "فرع التجمع الخامس",
        "code": "new-cairo",
        "phone": "+200000210002",
        "email": "new-cairo@tiaai.online",
        "address_line1": "التسعين الشمالي - التجمع الخامس",
        "city": "New Cairo",
        "hours": {
            0: [("10:00", "22:00")],
            1: [("10:00", "22:00")],
            2: [("10:00", "22:00")],
            3: [("10:00", "22:00")],
            5: [("10:00", "22:00")],
            6: [("10:00", "22:00")],
        },
    },
    {
        "key": "sheikh-zayed",
        "name": "فرع الشيخ زايد",
        "code": "sheikh-zayed",
        "phone": "+200000210003",
        "email": "sheikh-zayed@tiaai.online",
        "address_line1": "المحور المركزي - الشيخ زايد",
        "city": "Sheikh Zayed",
        "hours": {
            0: [("11:00", "21:00")],
            1: [("11:00", "21:00")],
            2: [("11:00", "21:00")],
            3: [("11:00", "21:00")],
            4: [("15:00", "21:00")],
            5: [("11:00", "21:00")],
            6: [("11:00", "21:00")],
        },
    },
]

SERVICES = [
    {
        "key": "laser-consultation",
        "name": "استشارة جلدية وليزر",
        "slug": "laser-dermatology-consultation",
        "category": "Consultation",
        "description": "تقييم أولي لتحديد الخطة المناسبة قبل الخدمات التي تحتاج مراجعة طبية.",
        "duration": 30,
        "price": 60000,
        "medical": True,
    },
    {
        "key": "laser-hair-removal",
        "name": "ليزر إزالة الشعر",
        "slug": "laser-hair-removal",
        "category": "Laser Hair Removal",
        "description": "جلسة ليزر إزالة شعر عامة عند عدم تحديد منطقة بعينها.",
        "duration": 45,
        "price": 150000,
        "medical": False,
    },
    {
        "key": "laser-hair-face",
        "name": "ليزر إزالة الشعر - وجه كامل",
        "slug": "laser-hair-removal-face",
        "category": "Laser Hair Removal",
        "description": "جلسة ليزر إزالة الشعر للوجه الكامل.",
        "duration": 20,
        "price": 65000,
        "medical": False,
    },
    {
        "key": "laser-hair-underarm",
        "name": "ليزر إزالة الشعر - إبط",
        "slug": "laser-hair-removal-underarm",
        "category": "Laser Hair Removal",
        "description": "جلسة ليزر إزالة الشعر لمنطقة الإبط.",
        "duration": 15,
        "price": 55000,
        "medical": False,
    },
    {
        "key": "laser-hair-bikini",
        "name": "ليزر إزالة الشعر - بكيني",
        "slug": "laser-hair-removal-bikini",
        "category": "Laser Hair Removal",
        "description": "جلسة ليزر إزالة الشعر لمنطقة البكيني.",
        "duration": 20,
        "price": 70000,
        "medical": False,
    },
    {
        "key": "laser-hair-underarm-bikini",
        "name": "ليزر إزالة الشعر - إبط وبكيني",
        "slug": "laser-hair-removal-underarm-bikini",
        "category": "Laser Hair Removal",
        "description": "جلسة مجمعة للإبط والبكيني.",
        "duration": 30,
        "price": 110000,
        "medical": False,
    },
    {
        "key": "laser-hair-arms",
        "name": "ليزر إزالة الشعر - ذراعين كاملين",
        "slug": "laser-hair-removal-full-arms",
        "category": "Laser Hair Removal",
        "description": "جلسة ليزر إزالة الشعر للذراعين بالكامل.",
        "duration": 30,
        "price": 120000,
        "medical": False,
    },
    {
        "key": "laser-hair-legs",
        "name": "ليزر إزالة الشعر - رجلين كاملتين",
        "slug": "laser-hair-removal-full-legs",
        "category": "Laser Hair Removal",
        "description": "جلسة ليزر إزالة الشعر للرجلين بالكامل.",
        "duration": 45,
        "price": 180000,
        "medical": False,
    },
    {
        "key": "laser-hair-full-body-women",
        "name": "ليزر إزالة الشعر - جسم كامل سيدات",
        "slug": "laser-hair-removal-full-body-women",
        "category": "Laser Hair Removal",
        "description": "جلسة جسم كامل للسيدات.",
        "duration": 60,
        "price": 350000,
        "medical": False,
    },
    {
        "key": "laser-hair-back-chest-men",
        "name": "ليزر إزالة الشعر - ظهر وصدر رجال",
        "slug": "laser-hair-removal-back-chest-men",
        "category": "Laser Hair Removal",
        "description": "جلسة ليزر للظهر والصدر للرجال.",
        "duration": 60,
        "price": 300000,
        "medical": False,
    },
    {
        "key": "fractional-acne-scars",
        "name": "فراكشنال CO2 لآثار حب الشباب",
        "slug": "fractional-co2-acne-scars",
        "category": "Laser Skin",
        "description": "جلسة فراكشنال لآثار حب الشباب بعد التقييم المناسب.",
        "duration": 60,
        "price": 300000,
        "medical": True,
    },
    {
        "key": "fractional-scars",
        "name": "فراكشنال CO2 للندبات",
        "slug": "fractional-co2-scars",
        "category": "Laser Skin",
        "description": "جلسة فراكشنال للندبات حسب التقييم الطبي.",
        "duration": 60,
        "price": 350000,
        "medical": True,
    },
    {
        "key": "pigmentation-laser",
        "name": "ليزر التصبغات والبقع",
        "slug": "laser-pigmentation-spots",
        "category": "Laser Skin",
        "description": "جلسة ليزر للتصبغات والبقع بعد تقييم الحالة.",
        "duration": 45,
        "price": 220000,
        "medical": True,
    },
    {
        "key": "tattoo-removal",
        "name": "إزالة الوشم بالليزر",
        "slug": "laser-tattoo-removal",
        "category": "Laser Skin",
        "description": "جلسة إزالة وشم بالليزر؛ عدد الجلسات يحدد بعد التقييم.",
        "duration": 45,
        "price": 250000,
        "medical": True,
    },
    {
        "key": "vascular-laser",
        "name": "ليزر الشعيرات الدموية والاحمرار",
        "slug": "laser-vascular-redness",
        "category": "Laser Skin",
        "description": "جلسة ليزر للشعيرات الدموية والاحمرار بعد مراجعة طبية.",
        "duration": 45,
        "price": 250000,
        "medical": True,
    },
    {
        "key": "carbon-peel",
        "name": "كربون ليزر بيل",
        "slug": "carbon-laser-peel",
        "category": "Laser Skin",
        "description": "جلسة كربون ليزر للعناية بالبشرة.",
        "duration": 45,
        "price": 180000,
        "medical": False,
    },
    {
        "key": "laser-rejuvenation",
        "name": "ليزر تجديد البشرة",
        "slug": "laser-skin-rejuvenation",
        "category": "Laser Skin",
        "description": "جلسة ليزر لتجديد مظهر البشرة بعد تحديد ملاءمة الحالة.",
        "duration": 45,
        "price": 220000,
        "medical": True,
    },
    {
        "key": "deep-cleansing",
        "name": "تنظيف بشرة عميق",
        "slug": "deep-facial-cleansing",
        "category": "Facial",
        "description": "جلسة تنظيف بشرة عميق.",
        "duration": 60,
        "price": 120000,
        "medical": False,
    },
    {
        "key": "hydrafacial",
        "name": "هيدرافيشل",
        "slug": "hydrafacial",
        "category": "Facial",
        "description": "جلسة عناية وتنظيف وترطيب للبشرة.",
        "duration": 60,
        "price": 180000,
        "medical": False,
    },
    {
        "key": "microneedling",
        "name": "ديرمابن وميكرونيدلينج",
        "slug": "dermapen-microneedling",
        "category": "Skin Treatment",
        "description": "جلسة ميكرونيدلينج بعد تقييم ملاءمة البشرة.",
        "duration": 60,
        "price": 170000,
        "medical": True,
    },
    {
        "key": "chemical-peel",
        "name": "تقشير كيميائي طبي",
        "slug": "medical-chemical-peel",
        "category": "Skin Treatment",
        "description": "جلسة تقشير كيميائي باختيار النوع المناسب للحالة.",
        "duration": 45,
        "price": 150000,
        "medical": True,
    },
    {
        "key": "acne-review",
        "name": "تقييم وعلاج آثار حب الشباب",
        "slug": "acne-scar-treatment-review",
        "category": "Dermatology",
        "description": "تقييم آثار حب الشباب وتحديد نوع الجلسة المناسبة.",
        "duration": 30,
        "price": 60000,
        "medical": True,
    },
    {
        "key": "botox",
        "name": "بوتوكس",
        "slug": "botox",
        "category": "Injectables",
        "description": "موعد بوتوكس يتطلب مراجعة طبية قبل التنفيذ.",
        "duration": 45,
        "price": 350000,
        "medical": True,
    },
    {
        "key": "filler",
        "name": "فيلر",
        "slug": "dermal-filler",
        "category": "Injectables",
        "description": "موعد فيلر بعد تقييم طبي للمنطقة والكمية المناسبة.",
        "duration": 60,
        "price": 450000,
        "medical": True,
    },
    {
        "key": "skin-booster",
        "name": "سكين بوستر",
        "slug": "skin-booster",
        "category": "Injectables",
        "description": "جلسة سكين بوستر بعد المراجعة الطبية.",
        "duration": 45,
        "price": 350000,
        "medical": True,
    },
    {
        "key": "prp-skin",
        "name": "PRP للبشرة",
        "slug": "prp-skin",
        "category": "Regenerative",
        "description": "جلسة بلازما للبشرة بعد تقييم طبي.",
        "duration": 60,
        "price": 250000,
        "medical": True,
    },
    {
        "key": "prp-hair",
        "name": "PRP للشعر",
        "slug": "prp-hair",
        "category": "Hair Treatment",
        "description": "جلسة بلازما للشعر بعد تقييم السبب وخطة العلاج.",
        "duration": 60,
        "price": 250000,
        "medical": True,
    },
    {
        "key": "hair-mesotherapy",
        "name": "ميزوثيرابي للشعر",
        "slug": "hair-mesotherapy",
        "category": "Hair Treatment",
        "description": "جلسة ميزوثيرابي للشعر بعد المراجعة الطبية.",
        "duration": 45,
        "price": 200000,
        "medical": True,
    },
]

HAIR_LASER_KEYS = [
    "laser-hair-removal",
    "laser-hair-face",
    "laser-hair-underarm",
    "laser-hair-bikini",
    "laser-hair-underarm-bikini",
    "laser-hair-arms",
    "laser-hair-legs",
    "laser-hair-full-body-women",
    "laser-hair-back-chest-men",
]

DOCTORS = [
    {
        "key": "ahmed-mahmoud",
        "first_name": "أحمد",
        "last_name": "محمود",
        "email": "ahmed.mahmoud@tiaai.online",
        "phone": "+200000220001",
        "job_title": "طبيب جلدية وليزر",
        "specialization": "Dermatology, Laser & Aesthetic Medicine",
        "bio": "طبيب جلدية وليزر ضمن بيانات التشغيل الواقعية للبيئة غير الإنتاجية.",
        "branches": ["nasr-city"],
        "primary_branch": "nasr-city",
        "services": HAIR_LASER_KEYS + ["carbon-peel", "laser-rejuvenation"],
        "custom_services": {
            "laser-hair-removal": {"price": 140000},
        },
        "hours": {
            "nasr-city": {
                1: [("16:00", "19:00"), ("20:00", "22:00")],
                3: [("16:00", "22:00")],
                5: [("12:00", "18:00")],
                6: [("16:00", "22:00")],
            }
        },
    },
    {
        "key": "sara-adel",
        "first_name": "سارة",
        "last_name": "عادل",
        "email": "sara.adel@tiaai.online",
        "phone": "+200000220002",
        "job_title": "استشاري جلدية وليزر",
        "specialization": "Dermatology & Laser",
        "bio": "تركّز على التصبغات والندبات والإجراءات الليزرية التي تحتاج مراجعة طبية.",
        "branches": ["new-cairo"],
        "primary_branch": "new-cairo",
        "services": [
            "laser-consultation",
            "fractional-acne-scars",
            "fractional-scars",
            "pigmentation-laser",
            "tattoo-removal",
            "vascular-laser",
            "laser-rejuvenation",
            "chemical-peel",
            "acne-review",
        ],
        "custom_services": {},
        "hours": {
            "new-cairo": {
                0: [("14:00", "20:00")],
                2: [("14:00", "20:00")],
                3: [("12:00", "18:00")],
                5: [("10:00", "16:00")],
            }
        },
    },
    {
        "key": "mariam-hassan",
        "first_name": "مريم",
        "last_name": "حسن",
        "email": "mariam.hassan@tiaai.online",
        "phone": "+200000220003",
        "job_title": "طبيب جلدية وتجميل",
        "specialization": "Dermatology & Aesthetic Medicine",
        "bio": "تعمل بين فرعي مدينة نصر والتجمع في أيام مختلفة.",
        "branches": ["nasr-city", "new-cairo"],
        "primary_branch": "new-cairo",
        "services": [
            "laser-consultation",
            "laser-hair-removal",
            "laser-hair-face",
            "laser-hair-underarm",
            "laser-hair-bikini",
            "laser-hair-underarm-bikini",
            "laser-rejuvenation",
            "deep-cleansing",
            "hydrafacial",
            "microneedling",
            "chemical-peel",
        ],
        "custom_services": {},
        "hours": {
            "nasr-city": {
                0: [("10:00", "16:00")],
                2: [("10:00", "16:00")],
            },
            "new-cairo": {
                1: [("10:00", "16:00")],
                6: [("10:00", "16:00")],
            },
        },
    },
    {
        "key": "omar-khalil",
        "first_name": "عمر",
        "last_name": "خليل",
        "email": "omar.khalil@tiaai.online",
        "phone": "+200000220004",
        "job_title": "استشاري جلدية وليزر",
        "specialization": "Dermatology, Scars & Laser Procedures",
        "bio": "متاح في فرع الشيخ زايد فقط مع تركيز على الندبات وإزالة الوشم.",
        "branches": ["sheikh-zayed"],
        "primary_branch": "sheikh-zayed",
        "services": [
            "laser-consultation",
            "fractional-acne-scars",
            "fractional-scars",
            "tattoo-removal",
            "pigmentation-laser",
            "vascular-laser",
            "acne-review",
        ],
        "custom_services": {},
        "hours": {
            "sheikh-zayed": {
                1: [("15:00", "21:00")],
                3: [("15:00", "21:00")],
                4: [("15:00", "20:00")],
                6: [("15:00", "21:00")],
            }
        },
    },
    {
        "key": "nour-ali",
        "first_name": "نور",
        "last_name": "علي",
        "email": "nour.ali@tiaai.online",
        "phone": "+200000220005",
        "job_title": "طبيب تجميل وحقن",
        "specialization": "Aesthetic Medicine & Injectables",
        "bio": "تعمل في مدينة نصر والشيخ زايد في أيام مختلفة.",
        "branches": ["nasr-city", "sheikh-zayed"],
        "primary_branch": "nasr-city",
        "services": [
            "laser-consultation",
            "botox",
            "filler",
            "skin-booster",
            "prp-skin",
        ],
        "custom_services": {
            "botox": {"duration": 40, "price": 380000},
        },
        "hours": {
            "nasr-city": {
                0: [("17:00", "22:00")],
                5: [("17:00", "22:00")],
            },
            "sheikh-zayed": {
                2: [("16:00", "21:00")],
                3: [("16:00", "21:00")],
            },
        },
    },
    {
        "key": "youssef-samir",
        "first_name": "يوسف",
        "last_name": "سمير",
        "email": "youssef.samir@tiaai.online",
        "phone": "+200000220006",
        "job_title": "طبيب ليزر",
        "specialization": "Laser Medicine",
        "bio": "يعمل بين التجمع والشيخ زايد وله جدول مختلف في كل فرع.",
        "branches": ["new-cairo", "sheikh-zayed"],
        "primary_branch": "new-cairo",
        "services": HAIR_LASER_KEYS + [
            "pigmentation-laser",
            "tattoo-removal",
            "carbon-peel",
        ],
        "custom_services": {
            "laser-hair-back-chest-men": {"duration": 50, "price": 280000},
        },
        "hours": {
            "new-cairo": {
                1: [("16:00", "22:00")],
                6: [("16:00", "22:00")],
            },
            "sheikh-zayed": {
                0: [("11:00", "17:00")],
                4: [("15:00", "21:00")],
                5: [("11:00", "17:00")],
            },
        },
    },
    {
        "key": "hala-mostafa",
        "first_name": "هالة",
        "last_name": "مصطفى",
        "email": "hala.mostafa@tiaai.online",
        "phone": "+200000220007",
        "job_title": "طبيب جلدية وعناية بالبشرة",
        "specialization": "Dermatology & Skin Care",
        "bio": "تغطي جلسات العناية بالبشرة وبعض الإجراءات الطبية المساندة.",
        "branches": ["new-cairo", "sheikh-zayed"],
        "primary_branch": "new-cairo",
        "services": [
            "laser-consultation",
            "deep-cleansing",
            "hydrafacial",
            "carbon-peel",
            "microneedling",
            "chemical-peel",
            "prp-skin",
            "prp-hair",
            "hair-mesotherapy",
        ],
        "custom_services": {
            "hydrafacial": {"duration": 75, "price": 200000},
        },
        "hours": {
            "new-cairo": {
                2: [("10:00", "16:00")],
                5: [("16:00", "22:00")],
            },
            "sheikh-zayed": {
                1: [("11:00", "17:00")],
                6: [("11:00", "17:00")],
            },
        },
    },
]

SCENARIO_PATIENTS = [
    {
        "key": "busy-evening",
        "first": "أحمد",
        "last": "حجز مسائي",
        "phone": "+200000230001",
        "source": "website",
        "status": "active",
    },
    {
        "key": "pending-new-cairo",
        "first": "سلمى",
        "last": "موعد معلق",
        "phone": "+200000230002",
        "source": "instagram",
        "status": "active",
    },
    {
        "key": "injectables",
        "first": "منى",
        "last": "حقن تجميلي",
        "phone": "+200000230003",
        "source": "referral",
        "status": "active",
    },
    {
        "key": "cancelled-slot",
        "first": "ريم",
        "last": "موعد ملغي",
        "phone": "+200000230004",
        "source": "phone",
        "status": "active",
    },
    {
        "key": "history",
        "first": "نورهان",
        "last": "تاريخ زيارات",
        "phone": "+200000230005",
        "source": "walk_in",
        "status": "active",
    },
    {
        "key": "multiple-upcoming",
        "first": "كريم",
        "last": "أكثر من موعد",
        "phone": "+200000230006",
        "source": "whatsapp",
        "status": "active",
    },
    {
        "key": "blocked",
        "first": "عميل",
        "last": "محظور",
        "phone": "+200000230007",
        "source": "whatsapp",
        "status": "blocked",
    },
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Seed a realistic multi-branch aesthetic/laser clinic fixture into a non-production "
            "Linka workspace."
        )
    )
    parser.add_argument("--workspace-id", type=UUID, default=None)
    parser.add_argument("--workspace-slug", default="tia")
    parser.add_argument(
        "--keep-legacy-active",
        action="store_true",
        help="Do not deactivate old demo/regression clinic-core records.",
    )
    parser.add_argument(
        "--without-scenarios",
        action="store_true",
        help="Seed clinic core only; skip synthetic patient/appointment scenarios.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show the planned fixture without writing to PostgreSQL.",
    )
    return parser.parse_args()


def require_non_production() -> None:
    if settings.is_production or settings.environment.lower() == "production":
        raise RuntimeError(
            "Refusing to seed synthetic clinic data into ENVIRONMENT=production. "
            "Use onboarding/admin tools for real clinic data."
        )


def fixture_id(workspace_id: UUID, key: str) -> UUID:
    return uuid5(workspace_id, f"{FIXTURE_NAMESPACE}:{key}")


def normalize_lookup(value: str | None) -> str:
    text_value = unicodedata.normalize("NFKD", value or "")
    text_value = "".join(ch for ch in text_value if not unicodedata.combining(ch))
    translation = str.maketrans(
        {
            "أ": "ا",
            "إ": "ا",
            "آ": "ا",
            "ى": "ي",
            "ة": "ه",
            "ؤ": "و",
            "ئ": "ي",
            "ـ": " ",
        }
    )
    text_value = text_value.translate(translation).casefold()
    text_value = re.sub(r"[^\w\u0600-\u06ff]+", " ", text_value)
    return " ".join(text_value.split())


def parse_clock(value: str) -> time:
    return time.fromisoformat(value)


def upsert_by_id(db: Session, model, row_id: UUID, **values):
    row = db.get(model, row_id)
    if row is None:
        row = model(id=row_id, **values)
        db.add(row)
    else:
        for key, value in values.items():
            setattr(row, key, value)
    db.flush()
    return row


def find_workspace(db: Session, *, workspace_id: UUID | None, workspace_slug: str) -> Workspace:
    if workspace_id is not None:
        workspace = db.get(Workspace, workspace_id)
    else:
        workspace = db.scalar(select(Workspace).where(Workspace.slug == workspace_slug))
    if workspace is None:
        requested = str(workspace_id) if workspace_id else workspace_slug
        raise RuntimeError(f"Workspace {requested!r} was not found.")
    return workspace


def upsert_branch(db: Session, workspace: Workspace, spec: dict) -> Branch:
    branch = db.scalar(
        select(Branch).where(
            Branch.workspace_id == workspace.id,
            Branch.code == spec["code"],
        )
    )
    if branch is None:
        wanted = normalize_lookup(spec["name"])
        for candidate in db.scalars(select(Branch).where(Branch.workspace_id == workspace.id)):
            if normalize_lookup(candidate.name) == wanted:
                branch = candidate
                break
    values = {
        "workspace_id": workspace.id,
        "name": spec["name"],
        "code": spec["code"],
        "phone": spec["phone"],
        "email": spec["email"],
        "address_line1": spec["address_line1"],
        "address_line2": None,
        "city": spec["city"],
        "state": "Cairo" if spec["city"] in {"Cairo", "New Cairo"} else "Giza",
        "country_code": "EG",
        "timezone": "Africa/Cairo",
        "is_active": True,
    }
    if branch is None:
        branch = Branch(id=fixture_id(workspace.id, f"branch:{spec['key']}"), **values)
        db.add(branch)
    else:
        for key, value in values.items():
            setattr(branch, key, value)
    db.flush()
    return branch


def upsert_service(db: Session, workspace: Workspace, spec: dict) -> Service:
    service = db.scalar(
        select(Service).where(
            Service.workspace_id == workspace.id,
            Service.slug == spec["slug"],
        )
    )
    if service is None:
        wanted = normalize_lookup(spec["name"])
        for candidate in db.scalars(select(Service).where(Service.workspace_id == workspace.id)):
            if normalize_lookup(candidate.name) == wanted:
                service = candidate
                break
    values = {
        "workspace_id": workspace.id,
        "name": spec["name"],
        "slug": spec["slug"],
        "category": spec["category"],
        "description": spec["description"],
        "duration_minutes": spec["duration"],
        "buffer_before_minutes": 0,
        "buffer_after_minutes": 10 if spec["medical"] else 5,
        "price_minor": spec["price"],
        "currency": "EGP",
        "requires_medical_review": spec["medical"],
        "is_active": True,
    }
    if service is None:
        service = Service(id=fixture_id(workspace.id, f"service:{spec['key']}"), **values)
        db.add(service)
    else:
        for key, value in values.items():
            setattr(service, key, value)
    db.flush()
    return service


def _looks_fixture_contact(value: str | None) -> bool:
    text_value = (value or "").casefold()
    return any(marker in text_value for marker in ("tia.example", "tia.local", "regression", "demo"))


def upsert_doctor(db: Session, workspace: Workspace, spec: dict) -> tuple[Staff, Doctor]:
    first_name, last_name = normalize_doctor_name_parts(
        spec["first_name"], spec["last_name"]
    )
    wanted_name = normalize_lookup(f"{first_name} {last_name}")
    fixture_staff_id = fixture_id(workspace.id, f"staff:{spec['key']}")
    fixture_doctor_id = fixture_id(workspace.id, f"doctor:{spec['key']}")

    # Preserve the fixture Doctor identity first. Older seed runs could have
    # attached this deterministic Doctor id to a pre-existing Staff row after
    # matching by display name. Reusing that Staff row keeps historical
    # appointments attached to the same canonical Doctor and avoids creating a
    # second active doctor just to repair presentation data.
    fixture_doctor = db.scalar(
        select(Doctor).where(
            Doctor.workspace_id == workspace.id,
            Doctor.id == fixture_doctor_id,
        )
    )
    staff = None
    if fixture_doctor is not None:
        staff = db.scalar(
            select(Staff).where(
                Staff.workspace_id == workspace.id,
                Staff.id == fixture_doctor.staff_id,
            )
        )
        if staff is None:
            raise RuntimeError(
                "Realistic fixture doctor points to a missing Staff row: "
                f"doctor={fixture_doctor.id}, staff={fixture_doctor.staff_id}."
            )

    # Newer fixture runs also have a deterministic Staff id. Prefer it before
    # email/name fallback when there is no existing fixture Doctor identity.
    if staff is None:
        staff = db.scalar(
            select(Staff).where(
                Staff.workspace_id == workspace.id,
                Staff.id == fixture_staff_id,
            )
        )

    email_owner = db.scalar(
        select(Staff).where(
            Staff.workspace_id == workspace.id,
            Staff.email == spec["email"],
        )
    )
    if staff is None:
        staff = email_owner

    # Human-readable name is deliberately the last fallback. A name is not a
    # safe identity key and historical demo data may contain title/casing
    # variants of the same doctor.
    if staff is None:
        for candidate in db.scalars(
            select(Staff).where(Staff.workspace_id == workspace.id)
        ):
            if normalize_lookup(f"{candidate.first_name} {candidate.last_name}") == wanted_name:
                staff = candidate
                break

    if staff is None:
        staff = Staff(
            id=fixture_staff_id,
            workspace_id=workspace.id,
            user_id=None,
            first_name=first_name,
            last_name=last_name,
            email=spec["email"],
            phone=spec["phone"],
            job_title=spec["job_title"],
            is_active=True,
        )
        db.add(staff)
        db.flush()
    else:
        staff.first_name = first_name
        staff.last_name = last_name

        # Contact values are fixture metadata, not doctor identity. If an old
        # duplicate Staff row already owns the synthetic fixture email, do not
        # steal it or merge the rows by name. Keep the canonical fixture Doctor
        # on its existing Staff row and let the legacy-doctor cleanup keep the
        # duplicate out of active booking/analytics.
        if email_owner is None or email_owner.id == staff.id:
            if not staff.email or _looks_fixture_contact(staff.email):
                staff.email = spec["email"]

        if not staff.phone or staff.phone.startswith("+200000"):
            staff.phone = spec["phone"]
        staff.job_title = spec["job_title"]
        staff.is_active = True
        db.flush()

    doctor = db.scalar(
        select(Doctor).where(
            Doctor.workspace_id == workspace.id,
            Doctor.staff_id == staff.id,
        )
    )
    if doctor is None:
        # If the deterministic fixture Doctor already exists it must have been
        # resolved above, so creating it here is safe and remains idempotent.
        doctor = Doctor(
            id=fixture_doctor_id,
            workspace_id=workspace.id,
            staff_id=staff.id,
            specialization=spec["specialization"],
            license_number=None,
            bio=spec["bio"],
            booking_enabled=True,
            is_active=True,
        )
        db.add(doctor)
    else:
        doctor.specialization = spec["specialization"]
        doctor.bio = spec["bio"]
        doctor.booking_enabled = True
        doctor.is_active = True
    db.flush()
    return staff, doctor


def assert_unique_active_doctor_names(db: Session, workspace: Workspace) -> None:
    rows = db.execute(
        select(Doctor.id, Staff.first_name, Staff.last_name)
        .join(
            Staff,
            (Staff.workspace_id == Doctor.workspace_id) & (Staff.id == Doctor.staff_id),
        )
        .where(
            Doctor.workspace_id == workspace.id,
            Doctor.is_active.is_(True),
            Staff.is_active.is_(True),
        )
    ).all()
    by_name: dict[str, list[str]] = {}
    for row in rows:
        display = normalize_doctor_display_name(
            " ".join(part for part in (row.first_name, row.last_name) if part)
        )
        key = normalize_lookup(display)
        if key:
            by_name.setdefault(key, []).append(str(row.id))
    duplicates = {key: ids for key, ids in by_name.items() if len(ids) > 1}
    if duplicates:
        raise RuntimeError(
            "Realistic fixture produced duplicate active doctor names; clean the synthetic "
            f"workspace before continuing: {duplicates}"
        )


def deactivate_legacy_fixtures(db: Session, workspace: Workspace) -> dict[str, int]:
    counts = {"branches": 0, "services": 0, "doctors": 0}
    fixture_doctor_ids = {
        fixture_id(workspace.id, f"doctor:{spec['key']}")
        for spec in DOCTORS
    }
    for branch in db.scalars(select(Branch).where(Branch.workspace_id == workspace.id)):
        code = (branch.code or "").casefold()
        if code.startswith("demo-") or code.startswith("regression-"):
            if branch.is_active:
                branch.is_active = False
                counts["branches"] += 1

    for service in db.scalars(select(Service).where(Service.workspace_id == workspace.id)):
        slug = (service.slug or "").casefold()
        if slug.startswith("demo-") or slug.startswith("regression-"):
            if service.is_active:
                service.is_active = False
                counts["services"] += 1

    rows = db.execute(
        select(Doctor, Staff)
        .join(
            Staff,
            (Staff.workspace_id == Doctor.workspace_id) & (Staff.id == Doctor.staff_id),
        )
        .where(Doctor.workspace_id == workspace.id)
    ).all()
    for doctor, staff in rows:
        fields = " ".join(
            part or ""
            for part in (
                staff.email,
                staff.job_title,
                doctor.specialization,
                doctor.bio,
            )
        ).casefold()
        marked_legacy = "regression" in fields or "demo" in fields
        outside_fixture = doctor.id not in fixture_doctor_ids
        if marked_legacy or outside_fixture:
            if doctor.is_active or doctor.booking_enabled:
                doctor.is_active = False
                doctor.booking_enabled = False
                counts["doctors"] += 1

            # Only demo/regression staff records are disabled. A non-fixture
            # doctor may belong to a legitimate workspace member, so preserve
            # Staff.is_active while removing that doctor from this staging
            # clinic fixture's booking graph.
            if marked_legacy:
                staff.is_active = False

            for assignment in db.scalars(
                select(DoctorBranch).where(
                    DoctorBranch.workspace_id == workspace.id,
                    DoctorBranch.doctor_id == doctor.id,
                )
            ):
                assignment.is_active = False
            for assignment in db.scalars(
                select(DoctorService).where(
                    DoctorService.workspace_id == workspace.id,
                    DoctorService.doctor_id == doctor.id,
                )
            ):
                assignment.is_active = False
    db.flush()
    return counts


def replace_branch_hours(
    db: Session,
    workspace: Workspace,
    branches: dict[str, Branch],
) -> None:
    for spec in BRANCHES:
        branch = branches[spec["key"]]
        db.execute(
            delete(BranchWorkingHour).where(
                BranchWorkingHour.workspace_id == workspace.id,
                BranchWorkingHour.branch_id == branch.id,
            )
        )
        for weekday, intervals in spec["hours"].items():
            for start, end in intervals:
                db.add(
                    BranchWorkingHour(
                        id=fixture_id(
                            workspace.id,
                            f"branch-hours:{spec['key']}:{weekday}:{start}:{end}",
                        ),
                        workspace_id=workspace.id,
                        branch_id=branch.id,
                        weekday=weekday,
                        start_time=parse_clock(start),
                        end_time=parse_clock(end),
                    )
                )
    db.flush()


def replace_doctor_assignments(
    db: Session,
    workspace: Workspace,
    doctors: dict[str, Doctor],
    branches: dict[str, Branch],
    services: dict[str, Service],
) -> None:
    for spec in DOCTORS:
        doctor = doctors[spec["key"]]
        db.execute(
            delete(DoctorWorkingHour).where(
                DoctorWorkingHour.workspace_id == workspace.id,
                DoctorWorkingHour.doctor_id == doctor.id,
            )
        )

        existing_branches = list(
            db.scalars(
                select(DoctorBranch).where(
                    DoctorBranch.workspace_id == workspace.id,
                    DoctorBranch.doctor_id == doctor.id,
                )
            )
        )
        wanted_branch_ids = {branches[key].id for key in spec["branches"]}
        for assignment in existing_branches:
            assignment.is_active = assignment.branch_id in wanted_branch_ids
            assignment.is_primary = assignment.branch_id == branches[spec["primary_branch"]].id

        for branch_key in spec["branches"]:
            branch = branches[branch_key]
            assignment = db.scalar(
                select(DoctorBranch).where(
                    DoctorBranch.workspace_id == workspace.id,
                    DoctorBranch.doctor_id == doctor.id,
                    DoctorBranch.branch_id == branch.id,
                )
            )
            if assignment is None:
                assignment = DoctorBranch(
                    id=fixture_id(
                        workspace.id,
                        f"doctor-branch:{spec['key']}:{branch_key}",
                    ),
                    workspace_id=workspace.id,
                    doctor_id=doctor.id,
                    branch_id=branch.id,
                    is_primary=branch_key == spec["primary_branch"],
                    is_active=True,
                )
                db.add(assignment)
            else:
                assignment.is_active = True
                assignment.is_primary = branch_key == spec["primary_branch"]
        db.flush()

        existing_services = list(
            db.scalars(
                select(DoctorService).where(
                    DoctorService.workspace_id == workspace.id,
                    DoctorService.doctor_id == doctor.id,
                )
            )
        )
        wanted_service_ids = {services[key].id for key in spec["services"]}
        for assignment in existing_services:
            assignment.is_active = assignment.service_id in wanted_service_ids

        for service_key in spec["services"]:
            service = services[service_key]
            custom = spec["custom_services"].get(service_key, {})
            assignment = db.scalar(
                select(DoctorService).where(
                    DoctorService.workspace_id == workspace.id,
                    DoctorService.doctor_id == doctor.id,
                    DoctorService.service_id == service.id,
                )
            )
            values = {
                # Duration is service-owned for every doctor. Re-seeding also
                # clears any legacy doctor-specific duration overrides.
                "custom_duration_minutes": None,
                "custom_price_minor": custom.get("price"),
                "is_active": True,
            }
            if assignment is None:
                assignment = DoctorService(
                    id=fixture_id(
                        workspace.id,
                        f"doctor-service:{spec['key']}:{service_key}",
                    ),
                    workspace_id=workspace.id,
                    doctor_id=doctor.id,
                    service_id=service.id,
                    **values,
                )
                db.add(assignment)
            else:
                for key, value in values.items():
                    setattr(assignment, key, value)
        db.flush()

        for branch_key, day_map in spec["hours"].items():
            branch = branches[branch_key]
            for weekday, intervals in day_map.items():
                for start, end in intervals:
                    db.add(
                        DoctorWorkingHour(
                            id=fixture_id(
                                workspace.id,
                                f"doctor-hours:{spec['key']}:{branch_key}:{weekday}:{start}:{end}",
                            ),
                            workspace_id=workspace.id,
                            doctor_id=doctor.id,
                            branch_id=branch.id,
                            weekday=weekday,
                            start_time=parse_clock(start),
                            end_time=parse_clock(end),
                        )
                    )
    db.flush()


def upsert_booking_settings(db: Session, workspace: Workspace) -> BookingSettings:
    booking = db.scalar(
        select(BookingSettings).where(BookingSettings.workspace_id == workspace.id)
    )
    if booking is None:
        booking = BookingSettings(workspace_id=workspace.id)
        db.add(booking)
    booking.slot_interval_minutes = 15
    booking.minimum_notice_minutes = 60
    booking.booking_horizon_days = 120
    booking.cancellation_notice_minutes = 1440
    booking.allow_same_day_booking = True
    booking.require_confirmation = True
    booking.default_currency = "EGP"
    db.flush()
    return booking


def next_local_weekday(weekday: int, hour: int, minute: int = 0, *, min_days: int = 1) -> datetime:
    now_local = datetime.now(CAIRO_TZ)
    days = (weekday - now_local.weekday()) % 7
    if days < min_days:
        days += 7
    target = now_local.date() + timedelta(days=days)
    return datetime(target.year, target.month, target.day, hour, minute, tzinfo=CAIRO_TZ)


def previous_local_weekday(weekday: int, hour: int, minute: int = 0) -> datetime:
    now_local = datetime.now(CAIRO_TZ)
    days = (now_local.weekday() - weekday) % 7
    if days == 0:
        days = 7
    target = now_local.date() - timedelta(days=days)
    return datetime(target.year, target.month, target.day, hour, minute, tzinfo=CAIRO_TZ)


def cleanup_scenario_patients(db: Session, workspace: Workspace) -> None:
    patient_ids = [fixture_id(workspace.id, f"patient:{spec['key']}") for spec in SCENARIO_PATIENTS]
    db.execute(
        delete(Appointment).where(
            Appointment.workspace_id == workspace.id,
            Appointment.patient_id.in_(patient_ids),
        )
    )
    db.execute(
        delete(Patient).where(
            Patient.workspace_id == workspace.id,
            Patient.id.in_(patient_ids),
        )
    )
    db.flush()


def create_scenario_patients(
    db: Session,
    workspace: Workspace,
    branches: dict[str, Branch],
) -> dict[str, Patient]:
    result: dict[str, Patient] = {}
    for index, spec in enumerate(SCENARIO_PATIENTS):
        preferred = branches["new-cairo"] if index % 2 else branches["nasr-city"]
        patient = Patient(
            id=fixture_id(workspace.id, f"patient:{spec['key']}"),
            workspace_id=workspace.id,
            first_name=spec["first"],
            last_name=spec["last"],
            phone=spec["phone"],
            phone_normalized=spec["phone"],
            gender=None,
            birth_date=None,
            preferred_language="ar",
            preferred_branch_id=preferred.id,
            source=spec["source"],
            source_detail=FIXTURE_VERSION,
            status=spec["status"],
            marketing_consent=False,
            marketing_consent_at=None,
            last_contact_at=datetime.now(timezone.utc) - timedelta(hours=index + 1),
        )
        db.add(patient)
        result[spec["key"]] = patient
    db.flush()
    return result


def effective_service_values(
    db: Session,
    workspace: Workspace,
    doctor: Doctor,
    service: Service,
) -> tuple[int, int]:
    assignment = db.scalar(
        select(DoctorService).where(
            DoctorService.workspace_id == workspace.id,
            DoctorService.doctor_id == doctor.id,
            DoctorService.service_id == service.id,
            DoctorService.is_active.is_(True),
        )
    )
    duration = service.duration_minutes
    price = service.price_minor
    if assignment is not None:
        if assignment.custom_price_minor is not None:
            price = assignment.custom_price_minor
    return duration, price


def make_appointment(
    *,
    db: Session,
    workspace: Workspace,
    patient: Patient,
    branch: Branch,
    doctor: Doctor,
    service: Service,
    key: str,
    start_local: datetime,
    status: str,
    source: str,
) -> Appointment:
    if status not in APPOINTMENT_STATUSES:
        raise ValueError(
            f"Invalid synthetic appointment status {status!r}; "
            f"allowed={APPOINTMENT_STATUSES!r}"
        )
    if source not in APPOINTMENT_SOURCES:
        raise ValueError(
            f"Invalid synthetic appointment source {source!r}; "
            f"allowed={APPOINTMENT_SOURCES!r}"
        )

    duration, price = effective_service_values(db, workspace, doctor, service)
    start_at = start_local.astimezone(timezone.utc)
    end_at = start_at + timedelta(minutes=duration)
    row = Appointment(
        id=fixture_id(workspace.id, f"appointment:{key}"),
        workspace_id=workspace.id,
        patient_id=patient.id,
        branch_id=branch.id,
        doctor_id=doctor.id,
        service_id=service.id,
        lead_id=None,
        created_by_user_id=None,
        rescheduled_from_appointment_id=None,
        status=status,
        source=source,
        start_at=start_at,
        end_at=end_at,
        busy_start_at=start_at - timedelta(minutes=service.buffer_before_minutes),
        busy_end_at=end_at + timedelta(minutes=service.buffer_after_minutes),
        duration_minutes=duration,
        price_minor=price,
        currency=service.currency,
        customer_note=f"{FIXTURE_VERSION}:{key}",
        cancellation_reason=None,
        idempotency_key=f"{FIXTURE_VERSION}:{key}",
    )
    now = datetime.now(timezone.utc)
    if status == "confirmed":
        row.confirmed_at = now
    elif status == "completed":
        row.confirmed_at = now - timedelta(days=7)
        row.completed_at = now - timedelta(days=6)
    elif status == "cancelled":
        row.cancelled_at = now - timedelta(hours=2)
        row.cancellation_reason = "Synthetic realistic fixture cancellation"
    elif status == "no_show":
        row.confirmed_at = now - timedelta(days=7)
        row.no_show_at = now - timedelta(days=6)
    db.add(row)
    db.flush()
    return row


def create_scenario_appointments(
    db: Session,
    workspace: Workspace,
    patients: dict[str, Patient],
    branches: dict[str, Branch],
    doctors: dict[str, Doctor],
    services: dict[str, Service],
) -> dict[str, Appointment]:
    rows: dict[str, Appointment] = {}

    rows["ahmed-tuesday-18-busy"] = make_appointment(
        db=db,
        workspace=workspace,
        patient=patients["busy-evening"],
        branch=branches["nasr-city"],
        doctor=doctors["ahmed-mahmoud"],
        service=services["laser-hair-removal"],
        key="ahmed-tuesday-18-busy",
        start_local=next_local_weekday(1, 18, 0),
        status="confirmed",
        source="website",
    )
    rows["sara-wednesday-14-pending"] = make_appointment(
        db=db,
        workspace=workspace,
        patient=patients["pending-new-cairo"],
        branch=branches["new-cairo"],
        doctor=doctors["sara-adel"],
        service=services["pigmentation-laser"],
        key="sara-wednesday-14-pending",
        start_local=next_local_weekday(2, 14, 0),
        status="pending",
        source="instagram",
    )
    rows["nour-thursday-17-botox"] = make_appointment(
        db=db,
        workspace=workspace,
        patient=patients["injectables"],
        branch=branches["sheikh-zayed"],
        doctor=doctors["nour-ali"],
        service=services["botox"],
        key="nour-thursday-17-botox",
        start_local=next_local_weekday(3, 17, 0),
        status="confirmed",
        # Patient acquisition can be referral, but Appointment.source intentionally
        # follows the appointment channel enum; referral is represented as "other".
        source="other",
    )
    rows["ahmed-thursday-18-cancelled"] = make_appointment(
        db=db,
        workspace=workspace,
        patient=patients["cancelled-slot"],
        branch=branches["nasr-city"],
        doctor=doctors["ahmed-mahmoud"],
        service=services["laser-hair-underarm-bikini"],
        key="ahmed-thursday-18-cancelled",
        start_local=next_local_weekday(3, 18, 0),
        status="cancelled",
        source="phone",
    )
    rows["history-completed"] = make_appointment(
        db=db,
        workspace=workspace,
        patient=patients["history"],
        branch=branches["new-cairo"],
        doctor=doctors["mariam-hassan"],
        service=services["hydrafacial"],
        key="history-completed",
        start_local=previous_local_weekday(1, 11, 0),
        status="completed",
        source="walk_in",
    )
    rows["history-no-show"] = make_appointment(
        db=db,
        workspace=workspace,
        patient=patients["history"],
        branch=branches["sheikh-zayed"],
        doctor=doctors["omar-khalil"],
        service=services["laser-consultation"],
        key="history-no-show",
        start_local=previous_local_weekday(3, 16, 0),
        status="no_show",
        source="phone",
    )
    rows["multiple-upcoming-1"] = make_appointment(
        db=db,
        workspace=workspace,
        patient=patients["multiple-upcoming"],
        branch=branches["new-cairo"],
        doctor=doctors["mariam-hassan"],
        service=services["deep-cleansing"],
        key="multiple-upcoming-1",
        start_local=next_local_weekday(6, 11, 0),
        status="confirmed",
        source="whatsapp",
    )
    rows["multiple-upcoming-2"] = make_appointment(
        db=db,
        workspace=workspace,
        patient=patients["multiple-upcoming"],
        branch=branches["sheikh-zayed"],
        doctor=doctors["hala-mostafa"],
        service=services["prp-hair"],
        key="multiple-upcoming-2",
        start_local=next_local_weekday(1, 12, 0),
        status="pending",
        source="whatsapp",
    )
    return rows


def planned_summary() -> dict:
    return {
        "fixture_version": FIXTURE_VERSION,
        "branches": len(BRANCHES),
        "services": len(SERVICES),
        "doctors": len(DOCTORS),
        "scenario_patients": len(SCENARIO_PATIENTS),
        "coverage": {
            "single_branch_doctors": [
                spec["key"] for spec in DOCTORS if len(spec["branches"]) == 1
            ],
            "multi_branch_doctors": [
                spec["key"] for spec in DOCTORS if len(spec["branches"]) > 1
            ],
            "split_shift_doctors": ["ahmed-mahmoud"],
            "medical_review_services": sum(1 for spec in SERVICES if spec["medical"]),
            "non_medical_services": sum(1 for spec in SERVICES if not spec["medical"]),
        },
    }


def main() -> int:
    args = parse_args()
    require_non_production()

    if args.dry_run:
        print(json.dumps(planned_summary(), ensure_ascii=False, indent=2))
        return 0

    engine = create_engine(settings.database_url, pool_pre_ping=True)
    SessionLocal = sessionmaker(
        bind=engine,
        class_=Session,
        autoflush=False,
        expire_on_commit=False,
    )

    with SessionLocal() as db:
        workspace = find_workspace(
            db,
            workspace_id=args.workspace_id,
            workspace_slug=args.workspace_slug,
        )
        if not workspace.is_active:
            raise RuntimeError("Refusing to seed an inactive workspace.")

        try:
            legacy_counts = {"branches": 0, "services": 0, "doctors": 0}
            if not args.keep_legacy_active:
                legacy_counts = deactivate_legacy_fixtures(db, workspace)

            branches = {
                spec["key"]: upsert_branch(db, workspace, spec) for spec in BRANCHES
            }
            services = {
                spec["key"]: upsert_service(db, workspace, spec) for spec in SERVICES
            }
            doctors: dict[str, Doctor] = {}
            for spec in DOCTORS:
                _, doctor = upsert_doctor(db, workspace, spec)
                doctors[spec["key"]] = doctor

            assert_unique_active_doctor_names(db, workspace)

            replace_branch_hours(db, workspace, branches)
            replace_doctor_assignments(db, workspace, doctors, branches, services)
            booking = upsert_booking_settings(db, workspace)

            patients: dict[str, Patient] = {}
            appointments: dict[str, Appointment] = {}
            if not args.without_scenarios:
                cleanup_scenario_patients(db, workspace)
                patients = create_scenario_patients(db, workspace, branches)
                appointments = create_scenario_appointments(
                    db,
                    workspace,
                    patients,
                    branches,
                    doctors,
                    services,
                )

            db.commit()
        except IntegrityError as exc:
            db.rollback()
            print("Seed failed due to a PostgreSQL constraint:", file=sys.stderr)
            print(exc, file=sys.stderr)
            return 1
        except Exception:
            db.rollback()
            raise

        summary = planned_summary()
        summary.update(
            {
                "environment": settings.environment,
                "workspace_id": str(workspace.id),
                "workspace_slug": workspace.slug,
                "legacy_records_deactivated": legacy_counts,
                "booking_settings": {
                    "slot_interval_minutes": booking.slot_interval_minutes,
                    "minimum_notice_minutes": booking.minimum_notice_minutes,
                    "booking_horizon_days": booking.booking_horizon_days,
                    "cancellation_notice_minutes": booking.cancellation_notice_minutes,
                    "allow_same_day_booking": booking.allow_same_day_booking,
                    "require_confirmation": booking.require_confirmation,
                },
                "branch_ids": {key: str(row.id) for key, row in branches.items()},
                "doctor_ids": {key: str(row.id) for key, row in doctors.items()},
                "service_ids": {key: str(row.id) for key, row in services.items()},
                "scenario_patient_ids": {key: str(row.id) for key, row in patients.items()},
                "scenario_appointment_ids": {
                    key: str(row.id) for key, row in appointments.items()
                },
                "safety": {
                    "production_blocked": True,
                    "external_messages_created": False,
                    "synthetic_contact_numbers_only": True,
                },
            }
        )
        print("Linka realistic aesthetic clinic fixture is ready")
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
