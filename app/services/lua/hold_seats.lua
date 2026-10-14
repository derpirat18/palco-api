-- Apartado atomico de N butacas: todo o nada.
--
-- Redis es monohilo y ejecuta este script completo sin intercalar otros
-- comandos. Por eso mil peticiones simultaneas por la misma butaca se
-- resuelven sin carreras: la primera gana, las demas reciben el indice
-- de la butaca que fallo.
--
-- KEYS: una llave por butaca -> hold:{func:<id>}:<seat_id>
--       El hash tag {func:<id>} fuerza que todas caigan en el mismo slot
--       de Redis Cluster. Sin eso el script falla al escalar.
-- ARGV[1]: hold_id del solicitante
-- ARGV[2]: TTL en segundos
--
-- Devuelve 0 si aparto todas, o el indice (base 1) de la primera que
-- tiene OTRO dueño. Las que ya son de este hold_id no cuentan como
-- ocupadas: asi un reintento (doble clic, red que se corta) no le dice
-- al comprador que alguien mas aparto su propia butaca.

for i = 1, #KEYS do
  local dueno = redis.call('GET', KEYS[i])
  if dueno and dueno ~= ARGV[1] then
    return i
  end
end

-- NX: solo escribe las butacas libres. Las que ya eran suyas conservan
-- su vencimiento original; un reintento no regala tiempo extra.
for i = 1, #KEYS do
  redis.call('SET', KEYS[i], ARGV[1], 'EX', ARGV[2], 'NX')
end

return 0
