# -*- coding: utf-8 -*-

from __future__ import division

import csv
import json
import logging
import pkg_resources

from numpy import (ceil, fromiter, int16, logical_not as not_, logical_or as or_, logical_and as and_, maximum as max_,
    minimum as min_, round as round_, where, select, take)

import openfisca_france
from openfisca_core.periods import Instant

from ..base import *  # noqa  analysis:ignore
from .prestations_familiales.base_ressource import nb_enf

log = logging.getLogger(__name__)

zone_apl_by_depcom = None


class al_nb_personnes_a_charge(Variable):
    column = FloatCol
    entity_class = Familles
    label = u"Nombre de personne à charge au sens des allocations logement"

    def function(self, simulation, period):
        '''
        site de la CAF en 2011:

        # Enfant à charge
        Vous assurez financièrement l'entretien et asez la responsabilité
        affective et éducative d'un enfant, que vous ayez ou non un lien de
        parenté avec lui. Il est reconnu à votre charge pour le versement
        des aides au logement jusqu'au mois précédent ses 21 ans.
        Attention, s'il travaille, il doit gagner moins de 836,55 € par mois.

        # Parents âgés ou infirmes
        Sont à votre charge s'ils vivent avec vous et si leurs revenus 2009
        ne dépassent pas 10 386,59 € :
        * vos parents ou grand-parents âgés de plus de 65 ans ou d'au moins
        60 ans, inaptes au travail, anciens déportés,
        * vos proches parents infirmes âgés de 22 ans ou plus (parents,
        grand-parents, enfants, petits enfants, frères, soeurs, oncles,
        tantes, neveux, nièces).
        '''

        period = period.this_month
        age_holder = simulation.compute('age', period)
        age_max_enfant = simulation.legislation_at(period.start).fam.cf.age2

        def al_nb_enfants():
            autonomie_financiere_holder = simulation.compute('autonomie_financiere', period)
            age = self.split_by_roles(age_holder, roles = ENFS)
            autonomie_financiere = self.split_by_roles(autonomie_financiere_holder, roles = ENFS)
            age_min_enfant = simulation.legislation_at(period.start).fam.af.age1

            return nb_enf(age, autonomie_financiere, age_min_enfant, age_max_enfant - 1) # La limite sur l'age max est stricte.

        def al_nb_adultes_handicapes():

            # Variables à valeur pour un individu
            base_ressources_i = simulation.compute('prestations_familiales_base_ressources_individu', period).array
            inapte_travail = simulation.compute('inapte_travail', period).array
            taux_incapacite = simulation.compute('taux_incapacite', period).array
            age = age_holder.array

            # Parametres
            plafond_ressource = simulation.legislation_at(period.n_2.stop).minim.aspa.plaf_seul * 1.25
            taux_incapacite_minimum = 0.8

            adulte_handicape = (
                ((taux_incapacite > taux_incapacite_minimum) + inapte_travail) *
                (age >= age_max_enfant) *
                (base_ressources_i <= plafond_ressource)
            )

            return self.sum_by_entity(adulte_handicape)

        return period, al_nb_enfants() + al_nb_adultes_handicapes()


class aide_logement_base_ressources_eval_forfaitaire(Variable):
    column = FloatCol
    entity_class = Familles
    label = u"Base ressources en évaluation forfaitaire des aides au logement (R351-7 du CCH)"

    def function(self, simulation, period):
        period = period.this_month

        def eval_forfaitaire_salaries():
            salaire_imposable_holder = simulation.compute('salaire_imposable', period.offset(-1))
            salaire_imposable = self.sum_by_entity(salaire_imposable_holder, roles = [CHEF, PART])

            # Application de l'abattement pour frais professionnels
            params_abattement = simulation.legislation_at(period.start).ir.tspr.abatpro
            somme_salaires_mois_precedent = 12 * salaire_imposable
            montant_abattement = round_(
                min_(
                    max_(params_abattement.taux * somme_salaires_mois_precedent, params_abattement.min),
                    params_abattement.max
                    )
                )
            return max_(0, somme_salaires_mois_precedent - montant_abattement)

        def eval_forfaitaire_tns():
            last_july_first= Instant(
                (period.start.year if period.start.month >= 7 else period.start.year - 1,
                7, 1))
            smic_horaire_brut = simulation.legislation_at(last_july_first).cotsoc.gen.smic_h_b
            travailleur_non_salarie_holder = simulation.compute('travailleur_non_salarie', period);
            any_tns = self.any_by_roles(travailleur_non_salarie_holder)
            return any_tns * 1500 * smic_horaire_brut

        return period, max_(eval_forfaitaire_salaries(), eval_forfaitaire_tns())


class aide_logement_abattement_chomage_indemnise(Variable):
    column = FloatCol
    entity_class = Individus
    label = u"Montant de l'abattement pour personnes au chômage indemnisé (R351-13 du CCH)"

    def function(self, simulation, period):
        period = period.this_month
        chomage_net_m_1 = simulation.calculate('chomage_net', period.offset(-1))
        chomage_net_m_2 = simulation.calculate('chomage_net', period.offset(-2))
        revenus_activite_pro = simulation.calculate('salaire_imposable', period.n_2)
        taux_abattement = simulation.legislation_at(period.start).al.ressources.abattement_chomage_indemnise
        taux_frais_pro = simulation.legislation_at(period.start).ir.tspr.abatpro.taux

        abattement = and_(chomage_net_m_1 > 0, chomage_net_m_2 > 0) * taux_abattement * revenus_activite_pro
        abattement = round_((1 - taux_frais_pro) * abattement)

        return period, abattement


class aide_logement_abattement_depart_retraite(Variable):
    column = FloatCol
    entity_class = Individus
    label = u"Montant de l'abattement sur les salaires en cas de départ en retraite"

    def function(self, simulation, period):
        period = period.this_month
        retraite = simulation.calculate('activite', period) == 3
        activite_n_2 = simulation.calculate('salaire_imposable', period.n_2)
        retraite_n_2 = simulation.calculate('retraite_imposable', period.n_2)
        taux_frais_pro = simulation.legislation_at(period.start).ir.tspr.abatpro.taux

        abattement = 0.3 * activite_n_2 * (retraite_n_2 == 0) * retraite
        abattement = round_((1 - taux_frais_pro) * abattement)

        return period, abattement


class aide_logement_neutralisation_rsa(Variable):
    column = FloatCol
    entity_class = Familles
    label = u"Abattement sur les revenus n-2 pour les bénéficiaires du RSA"

    def function(self, simulation, period):
        period = period.this_month
        # Circular definition, as rsa depends on al.
        # We don't allow it, so default value of rsa will be returned if a recursion is detected.
        rsa_last_month = simulation.calculate('rsa', period.last_month, max_nb_cycles = 0)
        activite = simulation.compute('salaire_imposable', period.n_2)
        chomage = simulation.compute('chomage_imposable', period.n_2)
        activite_n_2 = self.sum_by_entity(activite)
        chomage_n_2 = self.sum_by_entity(chomage)
        taux_frais_pro = simulation.legislation_at(period.start).ir.tspr.abatpro.taux

        abattement = (activite_n_2 + chomage_n_2) * rsa_last_month
        abattement = round_((1 - taux_frais_pro) * abattement)

        return period, abattement

class aide_logement_base_ressources_defaut(Variable):
    column = FloatCol
    entity_class = Familles
    label = u"Base ressource par défaut des allocations logement"

    def function(self, simulation, period):
        period = period.this_month
        rev_coll_holder = simulation.compute('rev_coll', period.n_2)
        rev_coll = self.sum_by_entity(rev_coll_holder)
        biactivite = simulation.calculate('biactivite', period)
        Pr = simulation.legislation_at(period.start).al.ressources
        base_ressources_holder = simulation.compute('prestations_familiales_base_ressources_individu', period)
        base_ressources_parents = self.sum_by_entity(base_ressources_holder, roles = [CHEF, PART])
        abattement_chomage_indemnise_holder = simulation.compute('aide_logement_abattement_chomage_indemnise', period)
        abattement_chomage_indemnise = self.sum_by_entity(abattement_chomage_indemnise_holder, roles = [CHEF, PART])
        abattement_depart_retraite_holder = simulation.compute('aide_logement_abattement_depart_retraite', period)
        abattement_depart_retraite = self.sum_by_entity(abattement_depart_retraite_holder, roles = [CHEF, PART])
        neutralisation_rsa = simulation.calculate('aide_logement_neutralisation_rsa', period)
        abattement_ressources_enfant = simulation.legislation_at(period.n_2.stop).minim.aspa.plaf_seul * 1.25
        br_enfants = self.sum_by_entity(
            max_(0, base_ressources_holder.array - abattement_ressources_enfant), roles = ENFS)
        ressources = (
            base_ressources_parents + br_enfants + rev_coll -
            (abattement_chomage_indemnise + abattement_depart_retraite + neutralisation_rsa)
        )

        # Abattement forfaitaire pour double activité
        abattement_double_activite = biactivite * Pr.dar_1

        # Arrondi aux 100 euros supérieurs
        result = max_(ressources - abattement_double_activite, 0)

        return period, result


class aide_logement_base_ressources(Variable):
    column = FloatCol
    entity_class = Familles
    label = u"Base ressources des allocations logement"

    def function(self, simulation, period):
        period = period.this_month
        mois_precedent = period.offset(-1)
        last_day_reference_year = period.n_2.stop
        base_ressources_defaut = simulation.calculate('aide_logement_base_ressources_defaut', period)
        base_ressources_eval_forfaitaire = simulation.calculate(
            'aide_logement_base_ressources_eval_forfaitaire', period)
        en_couple = simulation.calculate('en_couple', period)
        aah_holder = simulation.compute('aah', mois_precedent)
        aah = self.sum_by_entity(aah_holder, roles = [CHEF, PART])
        age_holder = simulation.compute('age', period)
        age = self.split_by_roles(age_holder, roles = [CHEF, PART])
        smic_horaire_brut_n2 = simulation.legislation_at(last_day_reference_year).cotsoc.gen.smic_h_b
        salaire_imposable_holder = simulation.compute('salaire_imposable', period.offset(-1))
        somme_salaires = self.sum_by_entity(salaire_imposable_holder, roles = [CHEF, PART])

        plafond_eval_forfaitaire = 1015 * smic_horaire_brut_n2

        plafond_salaire_jeune_isole = simulation.legislation_at(period.start).al.ressources.dar_8
        plafond_salaire_jeune_couple = simulation.legislation_at(period.start).al.ressources.dar_9
        plafond_salaire_jeune = where(en_couple, plafond_salaire_jeune_couple, plafond_salaire_jeune_isole)

        neutral_jeune = or_(age[CHEF] < 25, and_(en_couple, age[PART] < 25))
        neutral_jeune &= somme_salaires < plafond_salaire_jeune

        eval_forfaitaire = base_ressources_defaut <= plafond_eval_forfaitaire
        eval_forfaitaire &= base_ressources_eval_forfaitaire > 0
        eval_forfaitaire &= aah == 0
        eval_forfaitaire &= not_(neutral_jeune)

        ressources = where(eval_forfaitaire, base_ressources_eval_forfaitaire, base_ressources_defaut)

        # Planchers de ressources pour étudiants
        # Seul le statut étudiant (et boursier) du demandeur importe, pas celui du conjoint
        Pr = simulation.legislation_at(period.start).al.ressources
        etudiant_holder = simulation.compute('etudiant', period)
        boursier_holder = simulation.compute('boursier', period)
        etudiant = self.split_by_roles(etudiant_holder, roles = [CHEF, PART])
        boursier = self.split_by_roles(boursier_holder, roles = [CHEF, PART])
        montant_plancher_ressources = max_(0, etudiant[CHEF] * Pr.dar_4 - boursier[CHEF] * Pr.dar_5)
        ressources = max_(ressources, montant_plancher_ressources)

        # Arrondi aux 100 euros supérieurs
        ressources = ceil(ressources / 100) * 100

        return period, ressources


class aide_logement_montant_brut(Variable):
    column = FloatCol
    entity_class = Familles
    label = u"Formule des aides aux logements en secteur locatif en montant brut avant CRDS"

    def function(self, simulation, period):
        period = period.this_month

        # Situation familiale
        en_couple = simulation.calculate('en_couple', period)
        enceinte_fam = simulation.calculate('enceinte_fam', period)
        couple = or_(en_couple, enceinte_fam) # le barème "couple" est utilisé pour les femmes enceintes isolées
        personne_seule = not_(couple)
        al_nb_pac = simulation.calculate('al_nb_personnes_a_charge', period)

        # Logement
        statut_occupation_logement = simulation.calculate('statut_occupation_logement_famille', period)
        loyer_holder = simulation.compute('loyer', period)
        loyer = self.cast_from_entity_to_roles(loyer_holder)
        loyer = self.filter_role(loyer, role = CHEF)
        coloc_holder = simulation.compute('coloc', period)
        coloc = self.any_by_roles(coloc_holder)
        logement_chambre_holder = simulation.compute('logement_chambre', period)
        chambre = self.any_by_roles(logement_chambre_holder)
        locataire = ((3 <= statut_occupation_logement) & (5 >= statut_occupation_logement)) | (statut_occupation_logement == 7)
        accedant = (statut_occupation_logement == 1)

        # Ressources
        aide_logement_base_ressources = simulation.calculate('aide_logement_base_ressources', period)

        # Parametres législatifs
        al = simulation.legislation_at(period.start).al
        pfam_n_2 = simulation.legislation_at(period.start.offset(-2, 'year')).fam

        def loyer_retenu():

            # loyer mensuel réel, multiplié par 2/3 pour les meublés
            L1 = round_(loyer * where(statut_occupation_logement == 5, 2 / 3, 1))

            zone_apl = simulation.calculate('zone_apl_famille', period)

            # Paramètres contenant les plafonds de loyer pour cette zone
            plafonds_by_zone = [[0] + [al.loyers_plafond[ 'zone' + str(zone) ][ 'L' + str(i) ] for zone in range(1, 4)] for i in range(1, 5)]
            L2_personne_seule = take(plafonds_by_zone[0], zone_apl)
            L2_couple = take(plafonds_by_zone[1], zone_apl)
            L2_famille = take(plafonds_by_zone[2], zone_apl) + (al_nb_pac > 1) * (al_nb_pac - 1) * take(plafonds_by_zone[3], zone_apl)

            L2 = select(
                [personne_seule * (al_nb_pac == 0) + chambre, al_nb_pac > 0],
                [L2_personne_seule, L2_famille],
                default = L2_couple
                )

            # taux à appliquer sur le loyer plafond
            coeff_chambre_colloc = select(
                [chambre, coloc],
                [al.loyers_plafond.chambre, al.loyers_plafond.colocation],
                default = 1)

            L2 = round_(L2 * coeff_chambre_colloc, 2)

            # loyer retenu
            L = min_(L1, L2)

            return L
        loyer_retenu = loyer_retenu()

        def depense_eligible():
            # Loyer chargé pris en compte par la CAF comme montant des dépenses liées au logement

            # forfait de charges
            P_fc = al.forfait_charges
            C = where(coloc,
                (personne_seule * 0.5 + couple) * P_fc.fc1 + al_nb_pac * P_fc.fc2,
                P_fc.fc1 + al_nb_pac * P_fc.fc2)

            # dépense éligible
            E = loyer_retenu + C

            return E
        depense_eligible = depense_eligible()

        def indice_ressources_Rp():
            # Indice de ressource utilisé par la CAF, en €
            # Différence entre les ressources du foyer et un revenu R0 de référence

            # ressources prises en compte
            R = aide_logement_base_ressources

            # Plafond RO
            R1 = al.rmi * (
                al.R1.taux1 * personne_seule * (al_nb_pac == 0) +
                al.R1.taux2 * couple * (al_nb_pac == 0) +
                al.R1.taux3 * (al_nb_pac == 1) +
                al.R1.taux4 * (al_nb_pac >= 2) +
                al.R1.taux5 * (al_nb_pac > 2) * (al_nb_pac - 2)
                )

            R2 = pfam_n_2.af.bmaf * (
                al.R2.taux4 * (al_nb_pac >= 2) +
                al.R2.taux5 * (al_nb_pac > 2) * (al_nb_pac - 2)
                )

            Ro = round_(12 * (R1 - R2) * (1 - al.autres.abat_sal))

            Rp = max_(0, R - Ro)

            return Rp

        def taux_famille():
            # Taux représentant la situation familiale. Plus il est faible, moins la famille a de personnes à charge.
            TF = (
                al.TF.taux1 * (personne_seule) * (al_nb_pac == 0) +
                al.TF.taux2 * (couple) * (al_nb_pac == 0) +
                al.TF.taux3 * (al_nb_pac == 1) +
                al.TF.taux4 * (al_nb_pac == 2) +
                al.TF.taux5 * (al_nb_pac == 3) +
                al.TF.taux6 * (al_nb_pac >= 4) +
                al.TF.taux7 * (al_nb_pac > 4) * (al_nb_pac - 4)
            )

            return TF

        def taux_loyer():
            # Taux obscur basé sur une comparaison du loyer retenu à un loyer de référence.

            L = loyer_retenu
            z2 = al.loyers_plafond.zone2

            # Loyer de référence
            L_Ref = (
                z2.L1 * (personne_seule) * (al_nb_pac == 0) +
                z2.L2 * (couple) * (al_nb_pac == 0) +
                z2.L3 * (al_nb_pac >= 1) +
                z2.L4 * (al_nb_pac > 1) * (al_nb_pac - 1)
                )

            RL = L / L_Ref

            # TODO: paramètres en dur ??
            TL = max_(
                max_(0, al.TL.taux2 * (RL - 0.45)),
                al.TL.taux3 * (RL - 0.75) + al.TL.taux2 * (0.75 - 0.45)
            )

            return TL

        def participation_personelle():
            # Participation du demandeur à ses dépenses de logement

            # Depense eligible
            E = depense_eligible

            # participatioion personnelle minimale
            Po = max_(al.pp.taux * E, al.pp.min)

            # Taux de participation
            Tp = taux_famille() + taux_loyer()

            # Indice ressources
            Rp = indice_ressources_Rp()

            Pp = Po + Tp * Rp

            return Pp

        al_locataire = max_(0, depense_eligible - participation_personelle()) * locataire

        # Montant minimal de versement
        al_locataire = al_locataire * (al_locataire >= al.autres.nv_seuil)

        # TODO: APL pour les accédants à la propriété
        al_accedants = 0 * accedant

        al = al_locataire + al_accedants

        return period, al


class aide_logement_montant(Variable):
    column = FloatCol
    entity_class = Familles
    label = u"Montant des aides au logement net de CRDS"

    def function(self, simulation, period):
        period = period.this_month
        aide_logement_montant_brut = simulation.calculate('aide_logement_montant_brut', period)
        crds_logement = simulation.calculate('crds_logement', period)
        montant = round_(aide_logement_montant_brut + crds_logement, 2)

        return period, montant


class alf(Variable):
    calculate_output = calculate_output_add
    column = FloatCol
    entity_class = Familles
    label = u"Allocation logement familiale"
    url = u"http://vosdroits.service-public.fr/particuliers/F13132.xhtml"

    def function(self, simulation, period):
        period = period.this_month
        aide_logement_montant = simulation.calculate('aide_logement_montant', period)
        al_nb_pac = simulation.calculate('al_nb_personnes_a_charge', period)
        statut_occupation_logement = simulation.calculate('statut_occupation_logement_famille', period)
        proprietaire_proche_famille = simulation.calculate('proprietaire_proche_famille', period)

        result = (al_nb_pac >= 1) * (statut_occupation_logement != 3) * not_(proprietaire_proche_famille) * aide_logement_montant
        return period, result


class als_non_etudiant(Variable):
    column = FloatCol
    entity_class = Familles
    label = u"Allocation logement sociale (non étudiante)"

    def function(self, simulation, period):
        period = period.this_month
        aide_logement_montant = simulation.calculate('aide_logement_montant', period)
        al_nb_pac = simulation.calculate('al_nb_personnes_a_charge', period)
        etudiant_holder = simulation.compute('etudiant', period)
        statut_occupation_logement = simulation.calculate('statut_occupation_logement_famille', period)
        proprietaire_proche_famille = simulation.calculate('proprietaire_proche_famille', period)

        etudiant = self.split_by_roles(etudiant_holder, roles = [CHEF, PART])
        return period, (
            (al_nb_pac == 0) * (statut_occupation_logement != 3) * not_(proprietaire_proche_famille) *
            not_(etudiant[CHEF] | etudiant[PART]) * aide_logement_montant
        )


class als_etudiant(Variable):
    calculate_output = calculate_output_add
    column = FloatCol
    entity_class = Familles
    label = u"Allocation logement sociale (étudiante)"
    url = u"https://www.caf.fr/actualites/2012/etudiants-tout-savoir-sur-les-aides-au-logement"

    def function(self, simulation, period):
        period = period.this_month
        aide_logement_montant = simulation.calculate('aide_logement_montant', period)
        al_nb_pac = simulation.calculate('al_nb_personnes_a_charge', period)
        etudiant_holder = simulation.compute('etudiant', period)
        statut_occupation_logement = simulation.calculate('statut_occupation_logement_famille', period)
        proprietaire_proche_famille = simulation.calculate('proprietaire_proche_famille', period)

        etudiant = self.split_by_roles(etudiant_holder, roles = [CHEF, PART])
        return period, (
            (al_nb_pac == 0) * (statut_occupation_logement != 3) * not_(proprietaire_proche_famille) *
            (etudiant[CHEF] | etudiant[PART]) * aide_logement_montant
        )


class als(Variable):
    calculate_output = calculate_output_add
    column = FloatCol
    entity_class = Familles
    label = u"Allocation logement sociale"
    url = u"http://vosdroits.service-public.fr/particuliers/F1280.xhtml"

    def function(self, simulation, period):
        period = period.this_month
        als_non_etudiant = simulation.calculate('als_non_etudiant', period)
        als_etudiant = simulation.calculate('als_etudiant', period)
        result = (als_non_etudiant + als_etudiant)

        return period, result


class apl(Variable):
    calculate_output = calculate_output_add
    column = FloatCol
    entity_class = Familles
    label = u" Aide personnalisée au logement"
    # (réservée aux logements conventionné, surtout des HLM, et financé par le fonds national de l'habitation)"
    url = u"http://vosdroits.service-public.fr/particuliers/F12006.xhtml",

    def function(self, simulation, period):
        period = period.this_month
        aide_logement_montant = simulation.calculate('aide_logement_montant', period)
        statut_occupation_logement = simulation.calculate('statut_occupation_logement_famille', period)

        return period, aide_logement_montant * (statut_occupation_logement == 3)


class aide_logement_non_calculable(Variable):
    column = EnumCol(
        enum = Enum([
            u"",
            u"primo_accedant",
            u"locataire_foyer"
        ]),
        default = 0
    )
    entity_class = Familles
    label = u"Aide au logement non calculable"

    def function(self, simulation, period):
        period = period.this_month
        statut_occupation_logement = simulation.calculate('statut_occupation_logement_famille', period)

        return period, (statut_occupation_logement == 1) * 1 + (statut_occupation_logement == 7) * 2


class aide_logement(Variable):
    column = FloatCol
    entity_class = Familles
    label = u"Aide au logement (tout type)"

    def function(self, simulation, period):
        period = period.this_month
        apl = simulation.calculate('apl', period)
        als = simulation.calculate('als', period)
        alf = simulation.calculate('alf', period)

        return period, max_(max_(apl, als), alf)


class crds_logement(Variable):
    calculate_output = calculate_output_add
    column = FloatCol
    entity_class = Familles
    label = u"CRDS des allocations logement"
    url = u"http://vosdroits.service-public.fr/particuliers/F17585.xhtml"

    def function(self, simulation, period):
        period = period.this_month
        aide_logement_montant_brut = simulation.calculate('aide_logement_montant_brut', period)
        crds = simulation.legislation_at(period.start).fam.af.crds
        return period, -aide_logement_montant_brut * crds


class statut_occupation_logement_individu(EntityToPersonColumn):
    entity_class = Individus
    label = u"Statut d'occupation de l'individu"
    variable = Menages.column_by_name['statut_occupation_logement']


class statut_occupation_logement_famille(PersonToEntityColumn):
    entity_class = Familles
    label = u"Statut d'occupation de la famille"
    role = CHEF
    variable = Individus.column_by_name['statut_occupation_logement_individu']


class zone_apl(Variable):
    column = EnumCol(
        enum = Enum([
            u"Non renseigné",
            u"Zone 1",
            u"Zone 2",
            u"Zone 3",
            ]),
        default = 2
        )
    entity_class = Menages
    label = u"Zone APL"

    def function(self, simulation, period):
        '''
        Retrouve la zone APL (aide personnalisée au logement) de la commune
        en fonction du depcom (code INSEE)
        '''
        period = period
        depcom = simulation.calculate('depcom', period)

        preload_zone_apl()
        default_value = 2
        return period, fromiter(
            (
                zone_apl_by_depcom.get(depcom_cell, default_value)
                for depcom_cell in depcom
                ),
            dtype = int16,
            )


def preload_zone_apl():
    global zone_apl_by_depcom
    if zone_apl_by_depcom is None:
        with pkg_resources.resource_stream(
                openfisca_france.__name__,
                'assets/apl/20110914_zonage.csv',
                ) as csv_file:
            csv_reader = csv.DictReader(csv_file)
            zone_apl_by_depcom = {
                # Keep only first char of Zonage column because of 1bis value considered equivalent to 1.
                row['CODGEO']: int(row['Zonage'][0])
                for row in csv_reader
                }
        # Add subcommunes (arrondissements and communes associées), use the same value as their parent commune.
        with pkg_resources.resource_stream(
                openfisca_france.__name__,
                'assets/apl/commune_depcom_by_subcommune_depcom.json',
                ) as json_file:
            commune_depcom_by_subcommune_depcom = json.load(json_file)
            for subcommune_depcom, commune_depcom in commune_depcom_by_subcommune_depcom.iteritems():
                zone_apl_by_depcom[subcommune_depcom] = zone_apl_by_depcom[commune_depcom]


class zone_apl_individu(EntityToPersonColumn):
    entity_class = Individus
    label = u"Zone apl de la personne"
    variable = zone_apl


class zone_apl_famille(PersonToEntityColumn):
    entity_class = Familles
    label = u"Zone apl de la famille"
    role = CHEF
    variable = zone_apl_individu
