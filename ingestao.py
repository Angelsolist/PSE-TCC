import sqlite3
import unicodedata
import pandas as pd
import re
import os

# Função legada de normalização trazida para o ETL
def normalizar_texto(valor):
    texto = str(valor).strip().lower()
    texto = unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode("ascii")
    return " ".join(texto.split())

def ingerir_indicadores_globais(arquivo, conexao):
    # Restaura a lógica original: procura por "Gráficos" ou usa a primeira
    nome_aba = "Gráficos" if "Gráficos" in arquivo.sheet_names else arquivo.sheet_names[0]
    print(f"Tentando ler indicadores globais da aba: {nome_aba}")
    
    df_geral = pd.read_excel(arquivo, sheet_name=nome_aba, header=None)
    
    score_texto = {}
    for col in df_geral.columns:
        serie = df_geral[col].dropna().astype(str)
        score_texto[col] = serie.str.contains(r"[A-Za-zÀ-ÿ]", regex=True).sum()
        
    if not score_texto:
        raise ValueError(f"A aba {nome_aba} parece estar vazia ou ilegível.")
        
    col_texto = max(score_texto, key=score_texto.get)

    melhor_coluna_valor = None
    melhor_score = -1
    for col in df_geral.columns:
        if col == col_texto: continue
        numerico = pd.to_numeric(df_geral[col], errors="coerce")
        score = numerico.notna().sum()
        if score > melhor_score:
            melhor_score = score
            melhor_coluna_valor = col
            
    if melhor_coluna_valor is None:
        raise ValueError(f"Não foi possível detectar a coluna numérica na aba {nome_aba}.")

    base = pd.DataFrame({
        "descricao": df_geral[col_texto].astype(str),
        "valor": pd.to_numeric(df_geral[melhor_coluna_valor], errors="coerce"),
    }).dropna(subset=["valor"])
    
    base["descricao_norm"] = base["descricao"].map(normalizar_texto)
    
    def buscar(termos):
        for _, row in base.iterrows():
            if all(termo in row["descricao_norm"] for termo in termos):
                return float(row["valor"])
        return None

    pactuadas = buscar(["escolas", "pactuadas"])
    atingidas = buscar(["atingidas", "pse"])
    prioritarias = buscar(["priorit", "ms"]) or buscar(["priorit"])
    
    if pactuadas is None or atingidas is None or prioritarias is None:
        raise ValueError(f"Os textos 'pactuadas', 'atingidas pse' ou 'prioritárias' não foram achados na aba {nome_aba}.")
    
    df_indicadores = pd.DataFrame([{
        'Pactuadas': pactuadas,
        'Atingidas': atingidas,
        'Prioritarias': prioritarias
    }])
    
    # Se falhar aqui, o Python vai gritar, não vai esconder o erro
    df_indicadores.to_sql("indicadores_globais", conexao, if_exists="replace", index=False)
    print("✓ Indicadores globais ingeridos com sucesso.")
    
    return nome_aba # Retorna o nome da aba para sabermos qual ignorar no próximo loop


def limpar_e_derreter_aba(df, nome_aba):
    df_view = df.copy()

    # 1. Busca dinâmica pelas âncoras (Meses e Cabeçalhos)
    idx_mes = None
    for idx, row in df_view.head(15).iterrows():
        if row.astype(str).str.contains('Data mes', case=False, na=False).any():
            idx_mes = idx
            break

    idx_cab = None
    for idx, row in df_view.head(15).iterrows():
        # Busca estrita: só para na linha se achar exatamente a estrutura de chaves
        if row.astype(str).str.contains(r'us gerencia|us gerência|inep|qtde ativ', case=False, na=False, regex=True).any():
            idx_cab = idx
            break

    if idx_cab is None:
        idx_cab = 0

    if idx_mes is not None:
        linha_meses = df_view.iloc[idx_mes].fillna("").astype(str)
    else:
        linha_meses = [""] * len(df_view.columns)

    linha_cabecalhos = df_view.iloc[idx_cab].fillna("").astype(str)

    # 2. Funde meses com métricas
    novas_colunas = []
    for cabecalho, mes in zip(linha_cabecalhos, linha_meses):
        cab_limpo = cabecalho.strip()
        mes_limpo = mes.strip()

        if mes_limpo.isdigit():
            novas_colunas.append(f"{cab_limpo} (Mês {mes_limpo})")
        else:
            novas_colunas.append(cab_limpo)

    # 3. Desambiguação de chaves
    colunas_vistas = {}
    colunas_unicas = []
    for col in novas_colunas:
        if not col or col == "nan":
            col = "Sem_Nome"
            
        if col in colunas_vistas:
            colunas_vistas[col] += 1
            colunas_unicas.append(f"{col}_{colunas_vistas[col]}")
        else:
            colunas_vistas[col] = 0
            colunas_unicas.append(col)

    df_limpo = df_view.iloc[idx_cab + 1:].reset_index(drop=True)
    df_limpo.columns = colunas_unicas
    
    colunas_validas = [col for col in df_limpo.columns if not col.startswith("Sem_Nome")]
    df_limpo = df_limpo[colunas_validas]
    df_limpo = df_limpo.dropna(axis=1, how='all')

    # 4. Padronização de Esquema e Tratamento de Células Mescladas ANTES do Melt
    # 4. Padronização de Esquema e Tratamento de Células Mescladas ANTES do Melt
    renames = {}
    vistos = set()
    for col in df_limpo.columns:
        # Puxa para minúsculo e arranca espaços em branco das bordas
        col_lower = str(col).lower().strip()
        
        if ('gerencia' in col_lower or 'gerência' in col_lower) and 'Us_gerencia' not in vistos:
            renames[col] = 'Us_gerencia'
            vistos.add('Us_gerencia')
        elif col_lower in ['us', 'u.s', 'unidade de saude', 'unidade de saúde'] and 'Us' not in vistos:
            renames[col] = 'Us'
            vistos.add('Us')
        elif 'inep' in col_lower and 'Inep' not in vistos:
            renames[col] = 'Inep'
            vistos.add('Inep')
    
    df_limpo = df_limpo.rename(columns=renames)

    # Injeta colunas faltantes se a aba for defectiva
    for col_obrigatoria in ['Us_gerencia', 'Us', 'Inep']:
        if col_obrigatoria not in df_limpo.columns:
            df_limpo[col_obrigatoria] = None

    # Definição estrita das chaves estruturais
    colunas_chave = ['Us_gerencia', 'Us', 'Inep']
    
    # As colunas de valor serão todas as que não forem chaves nem marcadores booleanos
    colunas_valor = [
        col for col in df_limpo.columns 
        if col not in colunas_chave
        and "sim" not in col.lower()
        and "não" not in col.lower()
    ]

    # --- O PONTO CRÍTICO DE CORREÇÃO (Forward Fill e Normalização Estrita) ---
    import numpy as np
    
    df_limpo['Us_gerencia'] = df_limpo['Us_gerencia'].replace(r'^\s*$', np.nan, regex=True).ffill()
    # Converte tudo para maiúsculo e arranca espaços fantasmas. Nulos viram tag visível.
    df_limpo['Us_gerencia'] = df_limpo['Us_gerencia'].fillna('NAO_INFORMADA').astype(str).str.strip().str.upper()

    df_limpo['Us'] = df_limpo['Us'].replace(r'^\s*$', np.nan, regex=True).ffill()
    df_limpo['Us'] = df_limpo['Us'].fillna('NAO_INFORMADA').astype(str).str.strip().str.upper()

    # Prevenção: Se a aba não tiver colunas de dados válidas, pula a ingestão
    if not colunas_valor:
        return pd.DataFrame()

    # 5. Transformação Relacional (Melt)
    df_relacional = df_limpo.melt(
        id_vars=colunas_chave,
        value_vars=colunas_valor,
        var_name="Metrica_Bruta",
        value_name="Valor"
    )

    # Extrai o mês e limpa a métrica
    df_relacional['Mes'] = df_relacional['Metrica_Bruta'].apply(
        lambda x: int(re.search(r'Mês (\d+)', x).group(1)) if re.search(r'Mês (\d+)', x) else None
    )
    df_relacional['Metrica'] = df_relacional['Metrica_Bruta'].str.replace(r' \(Mês \d+\)', '', regex=True)
    
    df_relacional = df_relacional.drop(columns=['Metrica_Bruta'])
    
    # Tratamento rigoroso da coluna de valores
    df_relacional['Valor'] = df_relacional['Valor'].astype(str).str.replace(',', '.', regex=False)
    df_relacional['Valor'] = pd.to_numeric(df_relacional['Valor'].replace(['-', ''], pd.NA), errors='coerce')
    df_relacional['Valor'] = df_relacional['Valor'].fillna(0)
    df_relacional['Acao_PSE'] = renomeia_abas_para_exibicao(nome_aba)

    return df_relacional[['Us_gerencia', 'Us', 'Inep', 'Valor', 'Mes', 'Metrica', 'Acao_PSE']]

def renomeia_abas_para_exibicao(aba):
    # Dicionário garante a relação exata e performática de chave -> valor
    mapeamento = {
        "antro": "Antropometria",
        "alim saudavel": "Alimentação saudável",
        "pracorporais": "Práticas corporais (atividades físicas)",
        "smental": "Saúde mental",
        "violências": "Prevenção de violências",
        "sbucal": "Saúde bucal",
        "sauditiva": "Saúde auditiva",
        "socular": "Saúde ocular",
        "eduamb": "Educação ambiental",
        "dengue": "Combate à dengue",
        "culturapaz": "Cidadania e direitos humanos",
        "ssexual": "Saúde sexual e reprodutiva",
        "vacina": "Situação vacinal",
        "negligenciados": "Agravos negligenciados",
        "álcool e drogas": "Dependência química (álcool e outras drogas)",
        "sem saude na escola": "Semana Saúde na Escola"
    }
    
    aba_lower = aba.lower()
    for chave, valor_renomeado in mapeamento.items():
        if chave in aba_lower:
            return valor_renomeado
            
    # Retorno de segurança (fallback). Se não achar correspondência, 
    # retorna o nome original da aba em vez de propagar 'None' para o banco.
    return aba
    


def criar_banco_e_ingerir():
    arquivo_excel = "dados_PSE.xlsx"
    if not os.path.exists(arquivo_excel):
        # Levantando erro real que trava o script, em vez de um print passivo
        raise FileNotFoundError(f"Arquivo '{arquivo_excel}' não encontrado no diretório atual.")

    conexao = sqlite3.connect("dados_pse.db")
    arquivo = pd.ExcelFile(arquivo_excel)
    
    # 1. Ingestão Macro
    aba_macro = ingerir_indicadores_globais(arquivo, conexao)
    
    # 2. Ingestão Micro (Todas as abas exceto a que usamos para os indicadores globais)
    abas_micro = [aba for aba in arquivo.sheet_names if aba != aba_macro] 

    nomes_renomeados = {"Antropometria", "Alimentação saudável", 
                        "Práticas corporais (atividades físicas)", "Saúde mental",
                        "Prevenção de violências", "Saúde bucal", "Saúde auditiva",
                        "Saúde ocular", "Educação ambiental", "Combate à dengue",
                        "Cidadania e direitos humanos", "Saúde sexual e reprodutiva",
                        "Situação vacinal", "Agravos negligenciados",
                        "Dependência química (álcool e outras drogas)",
                        "Semana Saúde na Escola"}

    tabelas_finais = []
    for aba in abas_micro:
        df_bruto = pd.read_excel(arquivo, sheet_name=aba, header=None)
        df_processado = limpar_e_derreter_aba(df_bruto, aba)
        if not df_processado.empty:
            tabelas_finais.append(df_processado)
        
    if tabelas_finais:
        df_final = pd.concat(tabelas_finais, ignore_index=True)
        df_final.to_sql("registros_pse", conexao, if_exists="replace", index=False)
        print(f"✓ Ingestão concluída. {len(df_final)} registros inseridos no banco detalhado.")
    else:
        print("Aviso: Nenhuma aba granular válida foi processada.")
        
    conexao.close()

if __name__ == "__main__":
    criar_banco_e_ingerir()