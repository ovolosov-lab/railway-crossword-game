# Добавить в main.py обработчики исключений, чтобы ошибка записывалась в журнал и 
# фронтенду возвращался в случае ошибки json-ответ с понятным описанием ошибки, если это был асинхронный запрос, ожидающий json-ответ, 
# или происходила переадресация с передачей описания ошибки в виде флэш-сообщения, записанного в куку в случае проблемы с авторизацией

@app.exception_handler(HTTPException)
async def custom_http_exception_handler(request: Request, exc: HTTPException):
    if exc.status_code == 401:
        logger.warning(f"HTTP Exception: status=401, detail={exc.detail}")
        content_type = request.headers.get("content-type", "")
        if "application/json" in content_type:
            return JSONResponse(
                status_code=exc.status_code,
                content={"error": "401", "detail": exc.detail}
            )        
        else:
            logger.info("Error occurred during user entering the game. Redirecting to '/'")
            response = RedirectResponse(url="/", status_code=303)
            response.set_cookie(key="flash_msg", value=quote(exc.detail if exc.detail else "Please login first"), httponly=True)
            return response 


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError) -> JSONResponse | RedirectResponse:
    readable_errors = []
    
    for error in exc.errors():
        err_type = error['type']
        err_field = None
        template = error['msg'] if error['msg'] else "Validation error"

        if err_type.startswith("value_error") or err_type.startswith("type_error"):
                err_field = ",".join(str(loc) for loc in error['loc'])
        err_field = "" if err_field is None else err_field.replace("body,","").replace("query,","").replace("path,","").replace("header,","").replace("cookie,","")

        readable_errors.append(template + (f" ({err_field})" if err_field != ""  else ""))

    final_msg = " | ".join(readable_errors)

    accept_header = request.headers.get("accept", "")
    
    if "application/json" in accept_header:
        return JSONResponse(
            status_code=422,
            content={"result": "error", "details": final_msg}
        )
    response = RedirectResponse(url="/", status_code=303)
    response.set_cookie(key="flash_msg", value=quote(final_msg), httponly=True)  
    return response


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.exception("Произошла непредвиденная ошибка!") 
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal Server Error"}
    )
