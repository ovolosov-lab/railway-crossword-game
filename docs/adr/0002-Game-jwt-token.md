from fastapi import HTTPException, Request
import jwt
from uuid import UUID
from datetime import datetime, timedelta
from config import settings
from schemas import UserInfo


# Функция создает токен пользователя
def create_access_token(data: dict) -> str:
    to_encode: dict = data.copy()
    expire: datetime = datetime.now() + timedelta(minutes=settings.access_token_expire_minutes)
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, settings.jwt_secret, algorithm=settings.jwt_algorithm)


# Функция-зависимость для проверки токена
# Нужен класс UserInfo(Schema) в schemas.py 
async def get_current_user(request: Request) -> UserInfo:
    # Достаем токен из httponly куки 'access_token'
    token = request.cookies.get("access_token")
    
    if not token:
        raise HTTPException(
            status_code=401,
            detail="You are not authorized"
        )
    
    try:
        # Декодируем и проверяем срок действия (exp)
        payload = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
        username: str = str(payload.get("username", ""))
        user_id: UUID = UUID(payload.get("user_id")) 
 
        if (username is None) or (username == "") or (userid == 0):
			# в main.py нужен обработчик исключений со status_code=401 и status_code=422, 
			# возвращающий фронтенду JSON с информацией об ошибке: @app.exception_handler(HTTPException), @app.exception_handler(RequestValidationError)
			# и, возможно, @app.exception_handler(Exception) для записи в журнал трудно улавливаемых ошибок 			
            raise HTTPException(status_code=401, detail="Token is invalid"  
        return UserInfo(user_id=user_id, username=username)
	except ValueError:
		raise HTTPException(
			status_code=status.HTTP_401_UNAUTHORIZED, detail="User id is missing from the token"
		)	
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token is expired")
		# Возможно, фронтенду пользователя, у которого токен expired не более чем на 30 минут, придется сделать новый запрос, например /refresh тобы получить новый токен 
    except jwt.PyJWTError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authorization error")
