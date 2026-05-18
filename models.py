from datetime import date, datetime, time, timedelta

from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import Index, UniqueConstraint, text
from werkzeug.security import check_password_hash, generate_password_hash

db = SQLAlchemy()

ACTIVE_APPOINTMENT_STATUSES = ("confirmed", "rescheduled")


class User(db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    full_name = db.Column(db.String(120), nullable=False)
    email = db.Column(db.String(120), nullable=False, unique=True, index=True)
    phone = db.Column(db.String(30), unique=True)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(20), nullable=False, index=True)
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    doctor_profile = db.relationship(
        "Doctor",
        back_populates="user",
        uselist=False,
        cascade="all, delete-orphan",
    )
    patient_profile = db.relationship(
        "Patient",
        back_populates="user",
        uselist=False,
        cascade="all, delete-orphan",
    )

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)


class Doctor(db.Model):
    __tablename__ = "doctors"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, unique=True)
    specialization = db.Column(db.String(120), nullable=False)
    room_number = db.Column(db.String(20))
    appointment_duration = db.Column(db.Integer, nullable=False, default=30)
    bio = db.Column(db.Text)

    user = db.relationship("User", back_populates="doctor_profile")
    schedules = db.relationship(
        "Schedule",
        back_populates="doctor",
        cascade="all, delete-orphan",
        order_by="Schedule.work_date, Schedule.start_time",
    )
    appointments = db.relationship(
        "Appointment",
        back_populates="doctor",
        cascade="all, delete-orphan",
        foreign_keys="Appointment.doctor_id",
        order_by="Appointment.appointment_date, Appointment.start_time",
    )

    @property
    def full_name(self):
        return self.user.full_name if self.user else "Врач"


class Patient(db.Model):
    __tablename__ = "patients"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, unique=True)
    birth_date = db.Column(db.Date)
    insurance_number = db.Column(db.String(50))

    user = db.relationship("User", back_populates="patient_profile")
    appointments = db.relationship(
        "Appointment",
        back_populates="patient",
        cascade="all, delete-orphan",
        foreign_keys="Appointment.patient_id",
        order_by="Appointment.appointment_date, Appointment.start_time",
    )

    @property
    def full_name(self):
        return self.user.full_name if self.user else "Пациент"


class Schedule(db.Model):
    __tablename__ = "schedules"
    __table_args__ = (
        UniqueConstraint(
            "doctor_id",
            "work_date",
            "start_time",
            "end_time",
            name="uq_schedule_period",
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    doctor_id = db.Column(db.Integer, db.ForeignKey("doctors.id"), nullable=False, index=True)
    work_date = db.Column(db.Date, nullable=False, index=True)
    start_time = db.Column(db.Time, nullable=False)
    end_time = db.Column(db.Time, nullable=False)
    slot_duration = db.Column(db.Integer, nullable=False, default=30)
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    notes = db.Column(db.String(255))

    doctor = db.relationship("Doctor", back_populates="schedules")
    appointments = db.relationship("Appointment", back_populates="schedule")

    @property
    def total_slots(self):
        start_dt = datetime.combine(self.work_date, self.start_time)
        end_dt = datetime.combine(self.work_date, self.end_time)
        total_minutes = max(int((end_dt - start_dt).total_seconds() // 60), 0)
        if not self.slot_duration:
            return 0
        return total_minutes // self.slot_duration


class Appointment(db.Model):
    __tablename__ = "appointments"
    __table_args__ = (
        Index(
            "uq_active_doctor_slot",
            "doctor_id",
            "appointment_date",
            "start_time",
            unique=True,
            sqlite_where=text("status != 'cancelled'"),
        ),
        Index(
            "uq_active_patient_slot",
            "patient_id",
            "appointment_date",
            "start_time",
            unique=True,
            sqlite_where=text("status != 'cancelled'"),
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    patient_id = db.Column(db.Integer, db.ForeignKey("patients.id"), nullable=False, index=True)
    doctor_id = db.Column(db.Integer, db.ForeignKey("doctors.id"), nullable=False, index=True)
    schedule_id = db.Column(db.Integer, db.ForeignKey("schedules.id"), index=True)
    appointment_date = db.Column(db.Date, nullable=False, index=True)
    start_time = db.Column(db.Time, nullable=False)
    end_time = db.Column(db.Time, nullable=False)
    duration_minutes = db.Column(db.Integer, nullable=False, default=30)
    status = db.Column(db.String(20), nullable=False, default="confirmed", index=True)
    reason = db.Column(db.String(255))
    notes = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(
        db.DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False,
    )

    patient = db.relationship("Patient", back_populates="appointments")
    doctor = db.relationship("Doctor", back_populates="appointments")
    schedule = db.relationship("Schedule", back_populates="appointments")

    @property
    def is_active(self):
        return self.status in ACTIVE_APPOINTMENT_STATUSES


def _next_workdays(total_days=6):
    days = []
    cursor = date.today()
    while len(days) < total_days:
        if cursor.weekday() < 5:
            days.append(cursor)
        cursor += timedelta(days=1)
    return days


def _create_user(full_name, email, role, password, phone=None):
    user = User(
        full_name=full_name,
        email=email.lower(),
        phone=phone,
        role=role,
    )
    user.set_password(password)
    return user


def seed_data():
    if User.query.first():
        return

    admin = _create_user(
        "Системный администратор",
        "admin@clinic.local",
        "admin",
        "admin123",
        "+998900000001",
    )

    doctor_user_1 = _create_user(
        "Др. Анна Каримова",
        "doctor1@clinic.local",
        "doctor",
        "doctor123",
        "+998900000011",
    )
    doctor_user_2 = _create_user(
        "Др. Сергей Иванов",
        "doctor2@clinic.local",
        "doctor",
        "doctor123",
        "+998900000012",
    )
    doctor_user_3 = _create_user(
        "Др. Марина Ахмедова",
        "doctor3@clinic.local",
        "doctor",
        "doctor123",
        "+998900000013",
    )

    patient_user_1 = _create_user(
        "Алишер Хамидов",
        "patient1@clinic.local",
        "patient",
        "patient123",
        "+998900000021",
    )
    patient_user_2 = _create_user(
        "Екатерина Смирнова",
        "patient2@clinic.local",
        "patient",
        "patient123",
        "+998900000022",
    )
    patient_user_3 = _create_user(
        "Нодира Усманова",
        "patient3@clinic.local",
        "patient",
        "patient123",
        "+998900000023",
    )

    therapist = Doctor(
        user=doctor_user_1,
        specialization="Терапевт",
        room_number="101",
        appointment_duration=30,
        bio="Первичный приём, профилактика и общая диагностика.",
    )
    cardiologist = Doctor(
        user=doctor_user_2,
        specialization="Кардиолог",
        room_number="204",
        appointment_duration=40,
        bio="Диагностика и контроль сердечно-сосудистых заболеваний.",
    )
    pediatrician = Doctor(
        user=doctor_user_3,
        specialization="Педиатр",
        room_number="115",
        appointment_duration=30,
        bio="Приём детей, профилактика и сезонные осмотры.",
    )

    patient_1 = Patient(
        user=patient_user_1,
        birth_date=date(1998, 4, 11),
        insurance_number="INS-001",
    )
    patient_2 = Patient(
        user=patient_user_2,
        birth_date=date(1989, 9, 17),
        insurance_number="INS-002",
    )
    patient_3 = Patient(
        user=patient_user_3,
        birth_date=date(2001, 1, 25),
        insurance_number="INS-003",
    )

    db.session.add_all(
        [
            admin,
            doctor_user_1,
            doctor_user_2,
            doctor_user_3,
            patient_user_1,
            patient_user_2,
            patient_user_3,
            therapist,
            cardiologist,
            pediatrician,
            patient_1,
            patient_2,
            patient_3,
        ]
    )

    workdays = _next_workdays()
    schedules = []
    for workday in workdays:
        schedules.extend(
            [
                Schedule(
                    doctor=therapist,
                    work_date=workday,
                    start_time=time(9, 0),
                    end_time=time(13, 0),
                    slot_duration=30,
                    notes="Утренний приём.",
                ),
                Schedule(
                    doctor=cardiologist,
                    work_date=workday,
                    start_time=time(10, 0),
                    end_time=time(15, 20),
                    slot_duration=40,
                    notes="Кардиологический приём.",
                ),
                Schedule(
                    doctor=pediatrician,
                    work_date=workday,
                    start_time=time(8, 30),
                    end_time=time(12, 30),
                    slot_duration=30,
                    notes="Приём детей и профилактика.",
                ),
            ]
        )

    db.session.add_all(schedules)
    db.session.commit()

    therapist_first = next(
        schedule
        for schedule in schedules
        if schedule.doctor_id == therapist.id and schedule.work_date == workdays[0]
    )
    cardiologist_second = next(
        schedule
        for schedule in schedules
        if schedule.doctor_id == cardiologist.id and schedule.work_date == workdays[1]
    )
    pediatrician_third = next(
        schedule
        for schedule in schedules
        if schedule.doctor_id == pediatrician.id and schedule.work_date == workdays[2]
    )

    appointment_1 = Appointment(
        patient=patient_1,
        doctor=therapist,
        schedule=therapist_first,
        appointment_date=therapist_first.work_date,
        start_time=time(9, 0),
        end_time=time(9, 30),
        duration_minutes=30,
        status="confirmed",
        reason="Плановый осмотр",
        notes="Подтверждение отправлено пациенту.",
    )
    appointment_2 = Appointment(
        patient=patient_2,
        doctor=cardiologist,
        schedule=cardiologist_second,
        appointment_date=cardiologist_second.work_date,
        start_time=time(10, 0),
        end_time=time(10, 40),
        duration_minutes=40,
        status="confirmed",
        reason="Контроль давления",
        notes="Напоминание будет отправлено за 24 часа.",
    )
    appointment_3 = Appointment(
        patient=patient_3,
        doctor=pediatrician,
        schedule=pediatrician_third,
        appointment_date=pediatrician_third.work_date,
        start_time=time(9, 30),
        end_time=time(10, 0),
        duration_minutes=30,
        status="confirmed",
        reason="Плановая консультация",
        notes="Подтверждение отправлено пациенту.",
    )

    db.session.add_all([appointment_1, appointment_2, appointment_3])
    db.session.commit()


def init_db(app):
    db.init_app(app)
    with app.app_context():
        db.create_all()
        seed_data()
