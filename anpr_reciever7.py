import cv2
import json
import base64
import numpy as np
import threading
import time
from queue import Queue, Empty
from datetime import datetime,date
from confluent_kafka import Consumer, KafkaException, KafkaError
from confluent_kafka.admin import AdminClient, NewTopic
import torch
import re
import psycopg2
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from ultralytics import YOLO
from paddleocr import PaddleOCR
from email import encoders
from io import BytesIO

# Database configuration (Update with actual credentials)
DB_CONFIG = {
    "dbname": "anpr",
    "user": "postgres",
    "password": "hello@123",
    "host": "localhost",
    "port": "5432",
}

# Email configuration (Update with actual SMTP credentials)
EMAIL_CONFIG = {
    "sender_email": "testingmukund@gmail.com",
    "sender_password": "hhgs vvan kphd wter",
    "smtp_server": "smtp.gmail.com",
    "smtp_port": 587,
}

# Load YOLOv8 model
model = YOLO("best.pt")

# Load PaddleOCR
ocr = PaddleOCR(use_angle_cls=True, lang="en")

# Set to store unique valid number plates
#valid_number_plates = set()

# Define valid Indian state codes
VALID_STATE_CODES = {
    "AP", "AR", "AS", "BR", "CG", "CH", "DD", "DL", "GA", "GJ", "HP", "HR", "JH", "JK",
    "KA", "KL", "LA", "LD", "MH", "ML", "MN", "MP", "MZ", "NL", "OD", "PB", "PY", "RJ",
    "SK", "TN", "TR", "TS", "UK", "UP", "WB"
}

# Regular expression for Indian vehicle number plates
INDIAN_NUMBER_PLATE_REGEX = r"^([A-Z]{2})(\d{2})([A-Z]{2})(\d{4})$"

# def record_violations(regnos):
#     try:
#         conn = psycopg2.connect(**DB_CONFIG)
#         cursor = conn.cursor()
#         cursor.execute("select ")

# PostgreSQL function to fetch emails for multiple numbers


# Kafka Configuration
KAFKA_BROKER = '192.168.29.89:9092'
TOPIC = 'video-stream'
GROUP_ID = 'video-consumer-group'

active_streams = {}
active_streams_lock = threading.Lock()
frame_buffers = {}

def verify_violations(reg_no,lat,long):
    today = date.today()
    fresh = False
    #fresh_violation_number_plates = set()
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        cursor = conn.cursor()
        
        cursor.execute("SELECT id FROM violations WHERE regno = %s AND date = %s AND lat = %s AND long = %s;", (reg_no,today,lat,long))
        result = cursor.fetchone()
        if result is None:
            cursor.execute("INSERT INTO violations (regno,lat,long) VALUES (%s,%s,%s);", (reg_no,lat,long))
            conn.commit()
            fresh = True         
        cursor.close()
        conn.close()
    except Exception as e:
        print(f"❌ Database Error from verify_violations: {e}")
    return fresh

def send_email_to_violator(reg_num,frame,lat,long):
    today = date.today()
    formatted_date = today.strftime("%B %d, %Y")
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        cursor = conn.cursor()
        cursor.execute("SELECT regno, email FROM contacts WHERE regno = %s;", (reg_num,))
        result = cursor.fetchone()
        recipient_email = result[1]
        try:
            image_filename = "violation.png"  # Change to "violation.png" for PNG
            _, img_encoded = cv2.imencode('.png', frame)  # Use '.png' for PNG
            img_bytes = img_encoded.tobytes()

            msg = MIMEMultipart()
            msg["Subject"] = "Traffic Violation Notice"
            msg["From"] = EMAIL_CONFIG["sender_email"]
            msg["To"] = recipient_email

            body = f"Vehicle {reg_num}: You have been fined for no-parking violation for parking your vehicle at the no-parking zone with coordinates ({lat},{long}) on {formatted_date}"
            msg.attach(MIMEText(body, "plain"))

            part = MIMEBase("application", "octet-stream")
            part.set_payload(img_bytes)
            encoders.encode_base64(part)
            part.add_header("Content-Disposition", f"attachment; filename={image_filename}")
            msg.attach(part)


            server = smtplib.SMTP(EMAIL_CONFIG["smtp_server"], EMAIL_CONFIG["smtp_port"])
            server.starttls()
            server.login(EMAIL_CONFIG["sender_email"], EMAIL_CONFIG["sender_password"])
            
           
            # msg = MIMEText(f"Vehicle {reg_num}: You have been fined for no-parking violation!")
            # msg["Subject"] = "Traffic Violation Notice"
            # msg["From"] = EMAIL_CONFIG["sender_email"]
            # msg["To"] = recipient_email
            
            server.sendmail(EMAIL_CONFIG["sender_email"], recipient_email, msg.as_string())
            print(f"📩 Email sent to {recipient_email} for vehicle {reg_num}")
            
            server.quit()
        except Exception as e:
            print(f"❌ Email Error: {e}")
    except Exception as e:
        print(f"❌ Database Error from send_email_to_violator : {e}")


def fetch_emails_from_db(regnos):
    emails = {}
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        cursor = conn.cursor()
        cursor.execute("SELECT regno, email FROM contacts WHERE regno = ANY(%s);", (list(regnos),))
        results = cursor.fetchall()
        for regno, email in results:
            emails[regno] = email
        cursor.close()
        conn.close()
    except Exception as e:
        print(f"❌ Database Error : {e}")
    return emails

# Function to send emails
def send_emails(email_dict):
    try:
        server = smtplib.SMTP(EMAIL_CONFIG["smtp_server"], EMAIL_CONFIG["smtp_port"])
        server.starttls()
        server.login(EMAIL_CONFIG["sender_email"], EMAIL_CONFIG["sender_password"])
        
        for regno, recipient_email in email_dict.items():
            msg = MIMEText(f"Vehicle {regno}: You have been fined!")
            msg["Subject"] = "Traffic Violation Notice"
            msg["From"] = EMAIL_CONFIG["sender_email"]
            msg["To"] = recipient_email
            
            server.sendmail(EMAIL_CONFIG["sender_email"], recipient_email, msg.as_string())
            print(f"📩 Email sent to {recipient_email} for vehicle {regno}")
        
        server.quit()
    except Exception as e:
        print(f"❌ Email Error: {e}")


def create_topic_if_missing():
    """Checks if the Kafka topic exists and creates it if missing."""
    admin_client = AdminClient({'bootstrap.servers': KAFKA_BROKER})
    topic_metadata = admin_client.list_topics(timeout=5)
    if TOPIC in topic_metadata.topics:
        print(f"✅ Topic '{TOPIC}' already exists.")
        return
    new_topic = NewTopic(TOPIC, num_partitions=3, replication_factor=1)
    admin_client.create_topics([new_topic])
    print(f"🚀 Created topic '{TOPIC}', waiting for it to be ready...")
    time.sleep(5)
    topic_metadata = admin_client.list_topics(timeout=5)
    if TOPIC in topic_metadata.topics:
        print(f"✅ Topic '{TOPIC}' is now available.")
    else:
        print(f"❌ Topic '{TOPIC}' was not created successfully. Check Kafka logs.")

def generate_output_filename(stream_id, ext="avi"):
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"ov_{stream_id}_{timestamp}.{ext}"

def video_worker_thread(stream_id, frame_queue, output_filename):
    #frames = []
    first_frame = None
    valid_number_plates = set()
    fresh_number_plates = set()
    lat_set = set()
    long_set = set()

    while True:
        try:
            message = frame_queue.get(timeout=1.0)
        except Empty:
            continue

        if message.get('frame_id', None) == -1:
            print(f"[Worker {stream_id}] End-of-stream received.")
            break

        if 'data' not in message:
            continue
        
        print(message['lat'])
        print(message['long'])

        lat_set.add(message['lat'])
        long_set.add(message['long'])

        try:
            frame_data = base64.b64decode(message['data'])
            np_frame = np.frombuffer(frame_data, dtype=np.uint8)
            frame = cv2.imdecode(np_frame, cv2.IMREAD_COLOR)
            
            if frame is None:
                print(f"[Worker {stream_id}] Error decoding frame {message['frame_id']}. Skipping...")
                continue

            

            if first_frame is None:
                first_frame = frame
            #frame_bgr = cv2.cvtColor(frame, cv2.COLOR_RGB2BGR)
            frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)  # Convert BGR to RGB
            
            
            
            #modification starts from here

            if first_frame is not None:
                height, width, _ = first_frame.shape
                fourcc = cv2.VideoWriter_fourcc(*'XVID')  # Use 'XVID' instead of 'H264'
                out = cv2.VideoWriter(output_filename, fourcc, 14, (width, height))
            
            else:
                print(f"[Worker {stream_id}] No frames to write. Video not saved.")
                break

            results = model(frame_rgb)
            for result in results:
                for box in result.boxes:
                    x1, y1, x2, y2 = map(int, box.xyxy[0].tolist())
                    #cropped_plate = frame[y1:y2, x1:x2]
                    cropped_plate = frame_rgb[y1:y2, x1:x2]

                    if cropped_plate.size == 0:
                        continue

                    # Convert to grayscale for better OCR accuracy
                    #gray_plate = cv2.cvtColor(cropped_plate, cv2.COLOR_BGR2GRAY)
                    gray_plate = cv2.cvtColor(cropped_plate, cv2.COLOR_RGB2GRAY)

                    # Perform OCR
                    ocr_result = ocr.ocr(gray_plate, cls=True)

                    # Extract detected text
                    detected_text = ""
                    if ocr_result and len(ocr_result)>0:
                        for line in ocr_result:
                            if line:
                                for word in line:
                                    detected_text += word[1][0].upper()+" "

                    detected_text = detected_text.replace(" ", "")
                    detected_text = detected_text.replace("-", "")
                    detected_text = detected_text.replace("INDIA","")
                    detected_text = detected_text.replace("IND","")
                    detected_text = detected_text.replace("IN","")
                    detected_text = detected_text.replace("ND","")
                    match = re.match(INDIAN_NUMBER_PLATE_REGEX, detected_text)

                    if match:
                        state_code = match.group(1)
                        if state_code in VALID_STATE_CODES:
                            print("Valid Number Plate Detected:", detected_text)
                            valid_number_plates.add(detected_text)
                            fresh_violation = verify_violations(detected_text,message['lat'],message['long'])
                            if fresh_violation:
                                fresh_number_plates.add(detected_text)
                                print('a')
                                #violation_frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                                violation_frame = frame_rgb
                                send_email_to_violator(detected_text,violation_frame,message['lat'],message['long'])
                                print('b')

                            
                            # Draw bounding box and text
                            # cv2.rectangle(frame, (x1, y1), (x2, y2), (0, 255, 0), 2)
                            # cv2.putText(frame, detected_text, (x1, y1 - 10), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 0), 2)
                            cv2.rectangle(frame_rgb, (x1, y1), (x2, y2), (0, 255, 0), 2)
                            cv2.putText(frame_rgb, detected_text, (x1, y1 - 10), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 0), 2)
                        else:
                            print("Invalid State Code:", detected_text)
                    else:
                        print("Invalid State Code:", detected_text)


            # Write frame to output video
            #out.write(frame)
            out.write(frame_rgb)

            # Display frame (optional)
            #cv2.imshow("Frame", frame)
            cv2.imshow("Frame", frame_rgb)
            if cv2.waitKey(1) & 0xFF == ord("q"):
                break



            else:
                print(f"[Worker {stream_id}] No frames to write. Video not saved.")


            
            #frames.append(frame_rgb)

            #frames.append(frame)

        except Exception as e:
            print(f"[Worker {stream_id}] Error processing frame {message['frame_id']}: {e}")
            continue
    
    if first_frame is not None:
        out.release()
        print(f"[Worker {stream_id}] Video saved as {output_filename}")
        #fresh_violation_number_plates = verify_violations(valid_number_plates,list(lat_set)[0],list(long_set)[0])
        # emails = fetch_emails_from_db(fresh_number_plates)
        # send_emails(emails)
        # Print all unique valid number plates detected
        print(lat_set)
        print(long_set)
        print("\n📋 List of Unique Valid Number Plates Detected:")
        for plate in valid_number_plates:
            print(plate)
        print("\n📋 List of Fresh Violations Detected:")
        for fresh_plate in fresh_number_plates:
            print(fresh_plate)
    
    with active_streams_lock:
        if stream_id in active_streams:
            del active_streams[stream_id]

    #customization ends here


def main():
    create_topic_if_missing()
    consumer = Consumer({
        'bootstrap.servers': KAFKA_BROKER,
        'group.id': GROUP_ID,
        'auto.offset.reset': 'earliest'
    })
    consumer.subscribe([TOPIC])
    
    print("📡 Main thread: Listening for video streams...")

    try:
        while True:
            msg = consumer.poll(timeout=1.0)
            if msg is None:
                continue

            if msg.error():
                if msg.error().code() == KafkaError._PARTITION_EOF:
                    continue
                elif msg.error().code() == KafkaError.UNKNOWN_TOPIC_OR_PART:
                    print("⚠️ Kafka topic not available. Retrying in 5 seconds...")
                    time.sleep(5)
                    continue
                else:
                    print("❌ Consumer error:", msg.error())
                    continue

            try:
                message = json.loads(msg.value().decode('utf-8'))
            except Exception as e:
                print("Failed to decode message:", e)
                continue

            stream_id = message.get('stream_id', 'default')
            frame_id = message.get('frame_id')
            lat = message.get('lat')
            long = message.get('long')
            

            if frame_id == -1:
                with active_streams_lock:
                    if stream_id in active_streams:
                        active_streams[stream_id].put(message)
                continue

            chunk_id = message.get('chunk_id', 0)
            total_chunks = message.get('total_chunks', 1)
            data = message.get('data', '')

            if stream_id not in frame_buffers:
                frame_buffers[stream_id] = {}
            if frame_id not in frame_buffers[stream_id]:
                frame_buffers[stream_id][frame_id] = {}

            frame_buffers[stream_id][frame_id][chunk_id] = data

            if len(frame_buffers[stream_id][frame_id]) == total_chunks:
                complete_message = {
                    "stream_id": stream_id, 
                    "frame_id": frame_id, 
                    "data": ''.join(frame_buffers[stream_id][frame_id].values()),
                    "lat": lat,
                    "long": long
                }
                
                with active_streams_lock:
                    if stream_id not in active_streams:
                        output_filename = generate_output_filename(stream_id)
                        frame_queue = Queue()
                        frame_queue.put(complete_message)
                        worker = threading.Thread(target=video_worker_thread, args=(stream_id, frame_queue, output_filename), daemon=True)
                        worker.start()
                        active_streams[stream_id] = frame_queue
                    else:
                        active_streams[stream_id].put(complete_message)

                del frame_buffers[stream_id][frame_id]

    except KeyboardInterrupt:
        print("Main thread: Shutting down on KeyboardInterrupt.")
    finally:
        consumer.close()
        print("Consumer closed. Exiting.")

if __name__ == "__main__":
    main()