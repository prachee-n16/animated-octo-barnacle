from flask import Flask, jsonify, request, Response, stream_with_context

from flask_cors import CORS
import intersystems_iris.dbapi._DBAPI as dbapi
from werkzeug.utils import secure_filename
from dotenv import load_dotenv
import os
import intersystems_iris.dbapi._DBAPI as dbapi
from deepgram import DeepgramClient, PrerecordedOptions
from langchain.embeddings.openai import OpenAIEmbeddings
from langchain_iris import IRISVector
import requests

load_dotenv()

app = Flask(__name__)
CORS(app)

# Configuration
BASETEN_API_KEY = os.getenv("BASETEN_API_KEY")
DEEPGRAM_API_KEY = os.getenv("DEEPGRAM_API_KEY")
MODEL_ID = "8w6yyp2q"

# Database configuration
NAMESPACE = "USER"
PORT = 1972
HOSTNAME = os.getenv('IRIS_HOSTNAME', 'localhost')
CONNECTION_STRING = f"{HOSTNAME}:{PORT}/{NAMESPACE}"
USERNAME = "demo"
PASSWORD = "demo"


# Initialize connections and embeddings
conn = dbapi.connect(CONNECTION_STRING, USERNAME, PASSWORD)
embeddings = OpenAIEmbeddings()
db = IRISVector(connection_string=f"iris://demo:demo@{CONNECTION_STRING}", embedding_function=embeddings)
cursor = conn.cursor()

# Helper functions
def update_memory_embedding(memory_id, description):
    embedding = embeddings.embed_query(description)
    cursor.execute("""
        UPDATE Memories 
        SET embedding = TO_VECTOR(?, double) 
        WHERE memory_id = ?
    """, [embedding, memory_id])
    cursor.commit()

def similarity_search(query, k=5):
    query_embedding = embeddings.embed_query(query)
    query_embedding_str = ','.join(map(str, query_embedding))
    
    cursor.execute("""
        SELECT memory_id, title, description, VECTOR_COSINE(embedding, TO_VECTOR(?, double)) as similarity
        FROM Memories
        ORDER BY similarity DESC
        LIMIT ?
    """, [query_embedding_str, k])
    return cursor.fetchall()

def rag_memory_retrieval(query):
    relevant_memories = similarity_search(query)
    context = "\n".join([f"Memory {i+1}: {mem[1]} - {mem[2]}" for i, mem in enumerate(relevant_memories)])
    return context

@app.route('/get_memory_context', methods=['POST'])
def get_memory_context():
    data = request.json
    if 'query' not in data:
        return jsonify({"error": "Query is required"}), 400
    
    context = rag_memory_retrieval(data['query'])
    return jsonify({"context": context}), 200

def call_llama_model(messages, stream=True):
    payload = {
        "messages": messages,
        "stream": stream,
        "max_tokens": 2048,
        "temperature": 0.8
    }

    response = requests.post(
        f"https://model-{MODEL_ID}.api.baseten.co/production/predict",
        headers={"Authorization": f"Api-Key {BASETEN_API_KEY}"},
        json=payload,
        stream=stream
    )

    return response

# Routes
@app.route('/get_user/<string:name>', methods=['GET'])
def get_user(name):
    cursor.execute("SELECT * FROM Users WHERE name = ?", [name])
    user = cursor.fetchone()
    if user:
        return jsonify({
            "user_id": user[0],
            "name": user[1],
            "date_of_birth": user[2],
            "medical_conditions": user[3]
        }), 200
    return jsonify({"error": "User not found"}), 404

@app.route('/chat', methods=['POST'])
def chat():
    data = request.json
    if 'query' not in data:
        return jsonify({"error": "Query is required"}), 400

    query = data['query']
    context = rag_memory_retrieval(query)

    messages = [
        {"role": "system", "content": "You are an AI assistant with access to the user's memories. Use the provided context to answer the user's query."},
        {"role": "user", "content": f"query: {query}, context: {context}"}
    ]

    def generate():
        response = call_llama_model(messages, stream=True)
        if response.status_code == 200:
            for chunk in response.iter_content(chunk_size=1024):
                if chunk:
                    yield chunk.decode('utf-8')  # Decode the bytes to string
        else:
            yield f"Error: {response.status_code} - {response.text}"

    return Response(stream_with_context(generate()), content_type='text/event-stream')

@app.route('/get_user_memories/<int:user_id>', methods=['GET'])
def get_user_memories(user_id):
    cursor.execute("SELECT * FROM Memories WHERE user_id = ? ORDER BY timestamp DESC", [user_id])
    memories = cursor.fetchall()
    memory_list = [{
        "memory_id": memory[0],
        "title": memory[2],
        "description": memory[4],
        "timestamp": memory[5]
    } for memory in memories]
    return jsonify(memory_list), 200

@app.route('/get_user_people/<int:user_id>', methods=['GET'])
def get_user_people(user_id):
    cursor.execute("SELECT * FROM People WHERE user_id = ?", [user_id])
    people = cursor.fetchall()
    people_list = [{
        "person_id": person[0],
        "name": person[2],
        "relationship": person[3],
        "description": person[4]
    } for person in people]
    return jsonify(people_list), 200

@app.route('/transcribe', methods=['POST'])
def transcribe_audio():
    if 'audio' not in request.files:
        return jsonify({'error': 'No audio file provided'}), 400
    audio_file = request.files['audio']
    temp_filename = secure_filename('temp_audio.wav')
    temp_filepath = os.path.join('/tmp', temp_filename)
    audio_file.save(temp_filepath)
    deepgram = DeepgramClient(DEEPGRAM_API_KEY)
    try:
        with open(temp_filepath, 'rb') as buffer_data:
            options = PrerecordedOptions(smart_format=True, model="nova-2", language="en-US")
            response = deepgram.listen.prerecorded.v('1').transcribe_file({'buffer': buffer_data}, options)
            transcription = response['results']['channels'][0]['alternatives'][0]['transcript']
            return jsonify({"transcription": transcription})
    except Exception as e:
        return jsonify({"Error": str(e)}), 500

@app.route('/insert_user', methods=['POST'])
def insert_user():
    data = request.json
    try:
        cursor.execute("""
            INSERT INTO Users (name, date_of_birth, medical_conditions, profile_picture) 
            VALUES (?, ?, ?, ?)
        """, [data['name'], data['date_of_birth'], data['medical_conditions'], data['profile_picture']])
        conn.commit()
        return jsonify({"message": "User added successfully", "user_id": cursor.lastrowid}), 201
    except Exception as e:
        conn.rollback()
        return jsonify({"error": str(e)}), 400

@app.route('/insert_memory', methods=['POST'])
def insert_memory():
    data = request.json
    embedding = embeddings.embed_query(data['description'])
    try:
        cursor.execute("""
            INSERT INTO Memories (user_id, title, image, description, embedding) 
            VALUES (?, ?, ?, ?, ?)
        """, [data['user_id'], data['title'], data['image'], data['description'], embedding])
        conn.commit()
        return jsonify({"message": "Memory added successfully", "memory_id": cursor.lastrowid}), 201
    except Exception as e:
        conn.rollback()
        return jsonify({"error": str(e)}), 400

@app.route('/add_person', methods=['POST'])
def add_person():
    data = request.json
    try:
        cursor.execute("""
            INSERT INTO People (user_id, name, relationship, description, picture) 
            VALUES (?, ?, ?, ?, ?)
        """, [data['user_id'], data['name'], data['relationship'], data['description'], data['picture']])
        conn.commit()
        return jsonify({"message": "Person added successfully", "person_id": cursor.lastrowid}), 201
    except Exception as e:
        conn.rollback()
        return jsonify({"error": str(e)}), 400

if __name__ == '__main__':
    app.run(debug=True)
    