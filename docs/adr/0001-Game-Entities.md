# Пример структуры In-Memory объектов, которую желательно использовать: (DEPRECATED)

class HintUsed(BaseModel):
    category_revealed: bool = False
    first_letter_revealed: bool = False
    last_letter_revealed: bool = False

class WordPlacement(BaseModel):
    dictionary_word_id: int
    route_id: int
    position_index: int  # Порядковый номер слова на данном участке (0, 1, 2...)
    letter_count: int
    guessed_by_user_id: Uuid | None = None
    guessed_at: datetime | None = None

class PlayerGameState(BaseModel):
    user_id: Uuid
    start_city_id: int
    end_city_id: int
    chosen_route_ids: list[int]  # Список ID отрезков, составляющих путь
    hints: dict[int, HintUsed]  # key: dictionary_word_id -> какие подсказки уже куплены

class ActiveGame(BaseModel):
    game_id: Uuid
    started_at: datetime
    expires_at: datetime
    status: str = "active"  # active, finished
    winner_user_id: Uuid | None = None
    players: dict[Uuid, PlayerGameState]  # Состояние каждого игрока
    # Общий пул слов на общих участках дорог для синхронизации:
    shared_words: dict[int, list[WordPlacement]]  # key: route_id -> список слов на участке
