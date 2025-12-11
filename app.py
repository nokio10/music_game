import os
import json
import uuid
import string
from functools import wraps
from flask import Flask, render_template, request, Response, send_from_directory
from flask_socketio import SocketIO, emit, join_room

app = Flask(__name__)
app.config['SECRET_KEY'] = 'secret!'
socketio = SocketIO(app, cors_allowed_origins="*")

# --- CONFIG ---
ADMIN_USER = "admin"
ADMIN_PASS = "password"
MEDIA_FOLDER = os.path.join(os.getcwd(), 'media')

# --- STATE ---
class GameState:
    def __init__(self):
        self.game_id = None
        self.is_active = False
        self.current_q_index = -1
        self.questions = []
        self.players = {}
        self.player_names_map = {} 
        self.current_phase = 'idle' 
        self.inputs_enabled = False
        self.final_results = None
        self.auto_answer_task = None # Для хранения задачи таймера

game = GameState()

def load_questions():
    try:
        with open('questions.json', 'r', encoding='utf-8') as f:
            return json.load(f)
    except FileNotFoundError:
        return []

def normalize_text(text):
    if not text: return ""
    return text.translate(str.maketrans('', '', string.punctuation)).lower().strip()

# --- AUTH ---
def check_auth(username, password):
    return username == ADMIN_USER and password == ADMIN_PASS

def authenticate():
    return Response('Login Required', 401, {'WWW-Authenticate': 'Basic realm="Login Required"'})

def requires_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        auth = request.authorization
        if not auth or not check_auth(auth.username, auth.password):
            return authenticate()
        return f(*args, **kwargs)
    return decorated

# --- ROUTES ---
@app.route('/media/<path:filename>')
def serve_media(filename):
    return send_from_directory(MEDIA_FOLDER, filename)

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/admin')
@requires_auth
def admin():
    return render_template('admin.html')

# --- SOCKETS ---
@socketio.on('connect')
def on_connect():
    print('Client connected')

@socketio.on('join_game')
def on_join(data):
    name = data.get('name').strip()
    if not name: return
    
    if name in game.player_names_map:
        old_sid = game.player_names_map[name]
        score = 0
        if old_sid in game.players:
            score = game.players[old_sid]['score']
            del game.players[old_sid]
        game.player_names_map[name] = request.sid
        game.players[request.sid] = {'name': name, 'score': score, 'last_answer': None}
    else:
        game.player_names_map[name] = request.sid
        game.players[request.sid] = {'name': name, 'score': 0, 'last_answer': None}

    join_room('players')
    emit('game_status', _get_client_state(), to=request.sid)
    _broadcast_admin_info()

@socketio.on('submit_answer')
def on_answer(data):
    if not game.is_active or game.current_phase != 'question' or not game.inputs_enabled:
        return
    answer = data.get('answer')
    player = game.players.get(request.sid)
    if player:
        player['last_answer'] = answer
        _broadcast_admin_info()
        
        # --- ПРОВЕРКА: Все ли ответили? ---
        total_players = len(game.players)
        answered_count = sum(1 for p in game.players.values() if p['last_answer'] is not None)
        
        if total_players > 0 and answered_count == total_players:
            # Блокируем ввод, чтобы не спамили
            game.inputs_enabled = False
            # Запускаем таймер на клиентах
            socketio.emit('start_timer', {'seconds': 3}, to='players')
            # Запускаем фоновую задачу на сервере
            socketio.start_background_task(_auto_show_answer_task)

def _auto_show_answer_task():
    """Фоновая задача: ждет 3 секунды и раскрывает ответ."""
    socketio.sleep(3)
    # Проверяем, что фаза всё ещё 'question' (админ мог нажать "завершить" или "ответ" сам)
    if game.is_active and game.current_phase == 'question':
        # Используем контекст приложения, если нужно (для Flask-SocketIO обычно не обязательно, но безопасно)
        with app.app_context():
            _reveal_answer_logic()

# --- ADMIN ACTIONS ---
@socketio.on('admin_start_game')
def admin_start():
    game.game_id = str(uuid.uuid4())
    game.questions = load_questions()
    game.current_q_index = -1
    game.is_active = True
    game.current_phase = 'idle'
    game.inputs_enabled = False
    game.final_results = None
    
    game.players = {}
    game.player_names_map = {} 
    
    _broadcast_admin_info()
    socketio.emit('game_reset', to='players')

@socketio.on('admin_next_question')
def admin_next():
    if not game.is_active: return
    game.current_q_index += 1
    
    if game.current_q_index >= len(game.questions):
        admin_end()
    else:
        game.current_phase = 'question'
        game.inputs_enabled = False 
        for pid in game.players:
            game.players[pid]['last_answer'] = None
            
        q_data = game.questions[game.current_q_index]
        points = 1 if q_data['type'] == 'choice' else 2

        payload = {
            'type': q_data['type'],
            'options': q_data['options'],
            'question': q_data.get('question', ''), # <-- ДОБАВЛЕНО ПОЛЕ ВОПРОСА
            'index': game.current_q_index + 1,
            'inputs_enabled': False,
            'points': points
        }
        socketio.emit('new_question', payload, to='players')
        emit('play_audio', {'file': f"{q_data['id']}-1.mp3"}, to=request.sid)
        _broadcast_admin_info()

@socketio.on('admin_audio_finished')
def admin_audio_finished():
    if game.is_active and game.current_phase == 'question':
        game.inputs_enabled = True
        socketio.emit('allow_answers', to='players')

@socketio.on('admin_repeat_question')
def admin_repeat():
    if game.current_q_index >= 0 and game.current_q_index < len(game.questions):
        q_id = game.questions[game.current_q_index]['id']
        emit('play_audio', {'file': f"{q_id}-1.mp3"}, to=request.sid)

@socketio.on('admin_show_answer')
def admin_show_answer():
    # Вызываем общую логику (ручное нажатие)
    _reveal_answer_logic()

def _reveal_answer_logic():
    """Логика подсчета очков и переключения фазы."""
    if game.current_phase != 'question': return
    
    game.current_phase = 'answer'
    game.inputs_enabled = False # На всякий случай блокируем
    
    q_data = game.questions[game.current_q_index]
    correct_raw = q_data['answer']
    correct_norm = normalize_text(correct_raw)
    points_value = 1 if q_data['type'] == 'choice' else 2
    
    round_results_admin = [] 
    round_deltas = {}
    
    for pid, p_data in game.players.items():
        user_ans = p_data.get('last_answer')
        is_correct = False
        if user_ans:
            if q_data['type'] == 'choice':
                if user_ans == correct_raw: is_correct = True
            else:
                if normalize_text(user_ans) == correct_norm: is_correct = True
        
        earned = 0
        if is_correct:
            earned = points_value
            p_data['score'] += earned
        
        round_deltas[p_data['name']] = earned
        round_results_admin.append({
            'name': p_data['name'],
            'answer': user_ans if user_ans else "—",
            'is_correct': is_correct
        })
    
    leaderboard = []
    for pid, p in game.players.items():
        leaderboard.append({'name': p['name'], 'score': p['score']})
    leaderboard.sort(key=lambda x: x['score'], reverse=True)
            
    socketio.emit('show_answer_client', {
        'answer': correct_raw,
        'deltas': round_deltas,
        'leaderboard': leaderboard
    }, to='players')
    
    # Отправляем админу (через broadcast или напрямую, если вызвано из потока, request.sid может не быть)
    socketio.emit('play_audio', {'file': f"{q_data['id']}-2.mp3"}, to='admin_room')
    socketio.emit('round_results', round_results_admin, to='admin_room')
    _broadcast_admin_info()

@socketio.on('admin_end_game')
def admin_end():
    game.is_active = False
    game.current_phase = 'finished'
    
    leaderboard = []
    for pid, p in game.players.items():
        leaderboard.append({'name': p['name'], 'score': p['score']})
    leaderboard.sort(key=lambda x: x['score'], reverse=True)
    
    winners = []
    if leaderboard:
        max_score = leaderboard[0]['score']
        winners = [p for p in leaderboard if p['score'] == max_score]
    
    game.final_results = {
        'leaderboard': leaderboard,
        'winners': winners
    }
    
    socketio.emit('game_over', game.final_results)
    _broadcast_admin_info()

@socketio.on('admin_give_point')
def admin_give_point(data):
    if data.get('id') in game.players:
        game.players[data['id']]['score'] += 1
        _broadcast_admin_info()

@socketio.on('admin_take_point')
def admin_take_point(data):
    if data.get('id') in game.players:
        game.players[data['id']]['score'] -= 1
        _broadcast_admin_info()

# --- HELPERS ---
def _get_client_state():
    if not game.is_active: 
        if game.current_phase == 'finished' and game.final_results:
            return {'state': 'finished', 'final_results': game.final_results}
        return {'state': 'idle'}
        
    if game.current_phase == 'finished': 
        return {'state': 'finished', 'final_results': game.final_results}
    
    q_data = game.questions[game.current_q_index]
    points = 1 if q_data['type'] == 'choice' else 2
    return {
        'state': game.current_phase,
        'inputs_enabled': game.inputs_enabled,
        'question_data': {
            'type': q_data['type'],
            'options': q_data['options'],
            'question': q_data.get('question', ''), # <-- ДОБАВЛЕНО ПОЛЕ
            'index': game.current_q_index + 1,
            'points': points
        }
    }

def _broadcast_admin_info():
    players_list = [{'id': pid, 'name': p['name'], 'score': p['score'], 'has_answered': p['last_answer'] is not None} for pid, p in game.players.items()]
    players_list.sort(key=lambda x: x['score'], reverse=True)
    
    questions_status = []
    for i, q in enumerate(game.questions):
        status = 'waiting'
        if i < game.current_q_index: status = 'done'
        elif i == game.current_q_index: status = 'current'
        questions_status.append({'id': q['id'], 'status': status, 'type': q['type']})
    
    current_q_details = None
    if game.is_active and 0 <= game.current_q_index < len(game.questions):
        current_q_details = game.questions[game.current_q_index]

    info = {
        'players': players_list,
        'questions': questions_status,
        'game_active': game.is_active,
        'current_index': game.current_q_index,
        'phase': game.current_phase,
        'current_question': current_q_details,
        'final_results': game.final_results
    }
    socketio.emit('admin_update', info, to='admin_room')

@socketio.on('join_admin')
def join_admin_room():
    join_room('admin_room')
    _broadcast_admin_info()

if __name__ == '__main__':
    if not os.path.exists(MEDIA_FOLDER): os.makedirs(MEDIA_FOLDER)
    socketio.run(app, host='0.0.0.0', port=5000, debug=True)